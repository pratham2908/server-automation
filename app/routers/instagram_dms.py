"""An Instagram account's DMs, served from what we keep in Mongo.

Messages are read from Instagram once — live from the ``messages`` webhook, or by a catch-up that only
re-reads conversations whose ``updated_time`` moved — so opening the inbox costs no Instagram call at all.
Refresh runs the catch-up; sending still goes to Instagram, only when asked. Reading needs a token with
``instagram_manage_messages``; without it the catch-up comes back as a clear 403.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Literal

import requests
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field, model_validator

from app.database import get_db
from app.dependencies import verify_api_key
from app.services import dm_store
from app.services.dm_content import DmMessage
from app.services.dm_sender import deliver, r2_signer
from app.services.dm_stats import WINDOWS, awaiting_reply, dm_stats, flow, started_by
from app.services.follower_export import follower_handles
from app.services.instagram import InstagramService, graph_error_message
from app.services.instagram_dm_inbox import DmConversationOut, DmMessageOut, reply_window_ends_at
from app.services.instagram_tokens import PROVIDER_INSTAGRAM
from app.timezone import IST, assume_utc, now_ist, to_ist_iso

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/instagram/dms",
    tags=["instagram-dms"],
    dependencies=[Depends(verify_api_key)],
)

# A conversation is addressed by the other person's Instagram-scoped id.
PEER_ID = Path(pattern=r"^[0-9]{5,32}$")
SYNC_COOLDOWN = timedelta(seconds=30)
_syncing: set[str] = set()


class ConversationsOut(BaseModel):
    channel_id: str
    conversations: list[DmConversationOut]
    # Pass back as ``after`` for the next page; None on the last one.
    next_cursor: str | None = None
    # Conversations matching the filters; ``all_total`` counts every stored one.
    total: int = 0
    all_total: int = 0
    # For each filter, how many conversations each choice would show given the other filters chosen.
    facets: dict[str, dict[str, int]] = Field(default_factory=dict)
    # When the stored DMs were last checked against Instagram; None before the first catch-up.
    synced_at: str | None = None


class SyncOut(BaseModel):
    conversations: int
    read: int
    added: int
    failed: int = 0


class ThreadOut(BaseModel):
    channel_id: str
    conversation_id: str
    messages: list[DmMessageOut]
    # When we may no longer message this person; None once that has passed.
    reply_window_ends_at: str | None = None
    # "complete" when we hold the whole conversation; "partial" when Instagram hid older messages.
    history: str | None = None


class SendIn(BaseModel):
    recipient_id: str = Field(pattern=r"^[0-9]{5,32}$")
    # Plain text (Instagram caps one at 1,000 characters), or ``message`` for anything richer.
    text: str = Field(default="", max_length=1000)
    message: DmMessage | None = None

    @model_validator(mode="after")
    def _one_of(self) -> SendIn:
        if bool(self.text.strip()) == (self.message is not None):
            raise ValueError("send either text or a message, not both or neither")
        return self


class SendOut(BaseModel):
    channel_id: str
    recipient_id: str
    message_id: str
    # Every message the send became (media first); ``message_id`` is the last.
    message_ids: list[str] = Field(default_factory=list)


async def _service(channel_id: str, db: AsyncIOMotorDatabase) -> tuple[InstagramService, str]:
    """The channel's Instagram client and the id to address."""
    import app.main as main_mod

    await _channel(db, channel_id)
    manager = main_mod.instagram_service_manager
    resolved = await manager.get_service_and_user_id(channel_id, PROVIDER_INSTAGRAM) if manager else None
    if resolved is None:
        raise HTTPException(422, "This channel has no Instagram token")
    service, ig_user_id = resolved
    return service, ig_user_id


def _refused(exc: requests.RequestException) -> HTTPException:
    message = graph_error_message(exc)
    if "window" in message.casefold():
        return HTTPException(
            409, f"Instagram only allows a reply within 24 hours of their last message: {message[:200]}"
        )
    if "manage_messages" in message:
        return HTTPException(
            403,
            "This channel's Instagram token can't read messages: the instagram_manage_messages "
            "permission is missing. Reconnect it with that permission.",
        )
    return HTTPException(502, f"Instagram refused: {message[:300]}")


async def _channel(db: AsyncIOMotorDatabase, channel_id: str) -> dict[str, Any]:
    channel: dict[str, Any] | None = await db.channels.find_one({"channel_id": channel_id})
    if channel is None:
        raise HTTPException(404, "Channel not found")
    return channel


def _message_out(row: dict[str, Any], own_username: str | None, peer_username: str | None) -> DmMessageOut:
    return DmMessageOut(
        id=str(row["message_id"]),
        created_at=to_ist_iso(assume_utc(row["created_at"])),
        from_me=bool(row["from_me"]),
        sender=own_username if row["from_me"] else peer_username,
        text=row.get("text") or None,
        kind=row.get("kind") or "text",
        link=row.get("link"),
    )


def _iso(value: Any) -> str | None:
    return assume_utc(value).astimezone(IST).isoformat() if isinstance(value, datetime) else None


StartedFilter = Literal["them", "us", "comment_dm", "unknown"]
FlowFilter = Literal["two_way", "only_them", "only_us"]
AwaitingFilter = Literal["yes", "no"]
FollowsFilter = Literal["yes", "no"]


@router.get("", response_model=ConversationsOut)
async def list_dms(
    channel_id: str,
    limit: int = Query(20, ge=1, le=100),
    after: str | None = Query(None, pattern=r"^[0-9]{1,6}$", description="Cursor from the previous page"),
    started: StartedFilter | None = Query(None, description="Who sent the first message"),
    flow_is: FlowFilter | None = Query(None, alias="flow", description="Both sides wrote, or only one"),
    awaiting: AwaitingFilter | None = Query(None, description="yes: their message is the last word"),
    follows: FollowsFilter | None = Query(None, description="yes: they follow us, per the last followers export"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ConversationsOut:
    """Stored conversations, most recently active first, each with who started it, which way it goes and
    whether it waits on us; filterable on all three, with counts for each choice."""
    channel = await _channel(db, channel_id)
    own_username = str(channel.get("instagram_username") or "") or None
    conversations = await db.dm_conversations.find({"channel_id": channel_id}).to_list(length=None)
    messages = await db.dm_messages.find({"channel_id": channel_id, "deleted": {"$ne": True}}).to_list(length=None)
    comment_dm_ids = await _comment_dm_ids(db, channel_id)
    handles = await follower_handles(db, channel_id)
    threads: dict[str, list[dict[str, Any]]] = {}
    for m in sorted(messages, key=lambda r: assume_utc(r["created_at"])):
        threads.setdefault(m["peer_id"], []).append(m)
    every = []
    for conv in conversations:
        thread = threads.get(conv["peer_id"], [])
        if not thread:
            continue
        name = conv.get("peer_username")
        last = thread[-1]
        every.append(
            (
                assume_utc(last["created_at"]),
                DmConversationOut(
                    id=conv["peer_id"],
                    with_username=name,
                    with_id=conv["peer_id"],
                    updated_at=to_ist_iso(assume_utc(last["created_at"])),
                    last_message=_message_out(last, own_username, name),
                    started_by=started_by(conv, thread, comment_dm_ids),
                    message_count=len(thread),
                    flow=flow(thread),
                    awaiting_reply=awaiting_reply(thread),
                    follows_us=None if handles is None else str(name or "").lower() in handles,
                ),
            )
        )
    every.sort(key=lambda pair: pair[0], reverse=True)

    def key(c: DmConversationOut, dimension: str) -> str:
        if dimension == "started":
            return c.started_by or "unknown"
        if dimension == "flow":
            return c.flow or ""
        if dimension == "follows":
            return "yes" if c.follows_us else "no"
        return "yes" if c.awaiting_reply else "no"

    chosen: dict[str, str | None] = {"started": started, "flow": flow_is, "awaiting": awaiting}
    # Only offered once a followers export exists; before that nobody is known to follow.
    if handles is not None:
        chosen["follows"] = follows

    def passes(c: DmConversationOut, skip: str | None = None) -> bool:
        return all(value is None or key(c, dim) == value for dim, value in chosen.items() if dim != skip)

    facets: dict[str, dict[str, int]] = {}
    for dim in chosen:
        counts: dict[str, int] = {}
        for _, c in every:
            if passes(c, skip=dim):
                counts[key(c, dim)] = counts.get(key(c, dim), 0) + 1
        facets[dim] = counts
    shown = [c for _, c in every if passes(c)]
    offset = int(after or 0)
    return ConversationsOut(
        channel_id=channel_id,
        conversations=shown[offset : offset + limit],
        next_cursor=str(offset + limit) if offset + limit < len(shown) else None,
        total=len(shown),
        all_total=len(every),
        facets=facets,
        synced_at=_iso(channel.get(dm_store.SYNCED_FIELD)),
    )


async def _comment_dm_ids(db: AsyncIOMotorDatabase, channel_id: str) -> set[str]:
    """Ids of the DMs comment-to-DM sent, which mark the conversations it started."""
    rows = await db.comment_dm_events.find(
        {"channel_id": channel_id, "dm_message_id": {"$exists": True}}, {"dm_message_id": 1}
    ).to_list(length=None)
    return {str(r["dm_message_id"]) for r in rows if r.get("dm_message_id")}


@router.post("/sync", response_model=SyncOut)
async def sync_dms(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> SyncOut:
    """Check Instagram for anything the webhook missed: only conversations that changed are read."""
    channel = await _channel(db, channel_id)
    synced = channel.get(dm_store.SYNCED_FIELD)
    if channel_id in _syncing:
        raise HTTPException(409, "A refresh is already running")
    if isinstance(synced, datetime) and now_ist() - assume_utc(synced) < SYNC_COOLDOWN:
        # Checked moments ago; the stored copy is as fresh as another read would make it.
        return SyncOut(conversations=0, read=0, added=0)
    service, ig_user_id = await _service(channel_id, db)
    _syncing.add(channel_id)
    try:
        stats = await dm_store.catch_up(db, channel, service, ig_user_id, now_ist())
    except requests.RequestException as exc:
        raise _refused(exc) from exc
    finally:
        _syncing.discard(channel_id)
    return SyncOut(**stats)


@router.get("/stats")
async def dm_numbers(
    channel_id: str,
    window: Literal["24h", "7d", "30d", "all"] = Query("7d"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """Who starts conversations (them, us, comment-to-DM), replies both ways, who's waiting, daily series."""
    channel = await _channel(db, channel_id)
    conversations = await db.dm_conversations.find({"channel_id": channel_id}).to_list(length=None)
    messages = await db.dm_messages.find({"channel_id": channel_id}).to_list(length=None)
    commenters = await db.comment_log.find(
        {"channel_id": channel_id, "from_id": {"$nin": ["", None]}}, {"from_id": 1}
    ).to_list(length=None)
    stats = dm_stats(
        conversations,
        messages,
        comment_dm_ids=await _comment_dm_ids(db, channel_id),
        commenter_ids={str(r["from_id"]) for r in commenters},
        now=now_ist(),
        days=WINDOWS[window],
    )
    return {"window": window, "synced_at": _iso(channel.get(dm_store.SYNCED_FIELD)), **stats}


@router.get("/{peer_id}", response_model=ThreadOut)
async def get_dm_thread(
    channel_id: str,
    peer_id: str = PEER_ID,
    limit: int = Query(100, ge=1, le=500),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ThreadOut:
    """The newest *limit* stored messages with one person, oldest first."""
    channel = await _channel(db, channel_id)
    conv = await db.dm_conversations.find_one({"channel_id": channel_id, "peer_id": peer_id})
    if conv is None:
        raise HTTPException(404, "No conversation with this account")
    rows = await db.dm_messages.find({"channel_id": channel_id, "peer_id": peer_id, "deleted": {"$ne": True}}).to_list(
        length=None
    )
    rows.sort(key=lambda r: assume_utc(r["created_at"]))
    own_username = str(channel.get("instagram_username") or "") or None
    messages = [_message_out(r, own_username, conv.get("peer_username")) for r in rows[-limit:]]
    return ThreadOut(
        channel_id=channel_id,
        conversation_id=peer_id,
        messages=messages,
        reply_window_ends_at=reply_window_ends_at(messages, now_ist()),
        history=conv.get("history"),
    )


@router.post("/send", response_model=SendOut)
async def send_dm(channel_id: str, body: SendIn, db: AsyncIOMotorDatabase = Depends(get_db)) -> SendOut:
    """Send one text DM, only because this was called — nothing replies on its own.

    Instagram decides whether it may go: it must be within 24 hours of the person's last message.
    A refusal comes back with Instagram's own reason.
    """
    service, ig_user_id = await _service(channel_id, db)
    if body.message is not None:
        channel = await _channel(db, channel_id)
        try:
            ids = await deliver(
                db, service, ig_user_id, channel, body.recipient_id, body.message, now_ist(), r2_signer()
            )
        except requests.RequestException as exc:
            raise _refused(exc) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return SendOut(
            channel_id=channel_id, recipient_id=body.recipient_id, message_id=ids[-1] if ids else "", message_ids=ids
        )
    text = body.text.strip()
    try:
        message_id = await asyncio.to_thread(service.send_dm, ig_user_id, body.recipient_id, text)
    except requests.RequestException as exc:
        raise _refused(exc) from exc
    if message_id:
        await dm_store.record_sent(db, channel_id, body.recipient_id, message_id, text, now_ist())
    return SendOut(
        channel_id=channel_id, recipient_id=body.recipient_id, message_id=message_id, message_ids=[message_id]
    )
