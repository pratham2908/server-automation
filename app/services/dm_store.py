"""Instagram DMs kept in Mongo, so a message is read from Instagram once and never again.

- ``dm_messages``: one row per message, keyed by Instagram's message id.
- ``dm_conversations``: one row per person a channel talks to (``peer_id``, their Instagram-scoped id,
  which is also the ``from.id`` on their comments), with the conversation id and the ``updated_time``
  Instagram showed when we last read it.

Messages arrive two ways:

- the ``messages`` webhook, the moment one is sent either way (our own arrive as echoes);
- a catch-up that walks the conversation list newest first and stops at the first conversation whose
  ``updated_time`` hasn't moved, re-reading only the ones that did. Its first run is the backfill.

Instagram only gives details for a conversation's 20 newest messages, so whether we hold a
conversation's whole history (and so know who started it) is recorded as ``history``: ``complete``,
``partial``, or None until a read decides.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import requests
from dateutil.parser import isoparse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from app.logger import get_logger
from app.services.comment_dm_store import channel_for_webhook, own_ids
from app.services.instagram_dm_inbox import parse_message

logger = get_logger(__name__)

SOURCE_WEBHOOK = "webhook"
SOURCE_API = "api"
SOURCE_SENT = "sent"

HISTORY_COMPLETE = "complete"
HISTORY_PARTIAL = "partial"

SYNCED_FIELD = "dm_synced_at"
# Set once the first full read has finished: a person who first writes after it is a new conversation.
BACKFILLED_FIELD = "dm_backfilled_at"
# True while a catch-up runs, and after one that didn't finish cleanly.
PENDING_FIELD = "dm_catch_up_pending"

# Instagram allows 2 conversation-API calls a second per account.
CALL_SPACING_SECONDS = 0.5
# A webhook copy and an API copy of one message are matched on these if their ids ever differ.
SAME_MESSAGE_WINDOW = timedelta(seconds=5)


class DmEvent(BaseModel):
    """One message from the ``messages`` webhook, sent by either side."""

    account_id: str
    sender_id: str
    recipient_id: str
    message_id: str
    created_at: datetime
    text: str = ""
    kind: str = "text"
    link: str | None = None
    is_echo: bool = False
    is_deleted: bool = False


def _webhook_kind(message: Mapping[str, Any]) -> tuple[str, str | None]:
    if message.get("is_unsupported"):
        return "unsupported", None
    attachments = [a for a in message.get("attachments") or [] if isinstance(a, Mapping)]
    if attachments:
        first = attachments[0]
        link = (first.get("payload") or {}).get("url") if isinstance(first.get("payload"), Mapping) else None
        kind = {"share": "share", "ig_reel": "share", "reel": "share", "story_mention": "story"}.get(
            str(first.get("type")), "attachment"
        )
        return kind, link if isinstance(link, str) else None
    reply_to = message.get("reply_to")
    if isinstance(reply_to, Mapping) and reply_to.get("story"):
        story = reply_to["story"]
        return "story", story.get("url") if isinstance(story, Mapping) else None
    return "text", None


def parse_message_webhook(payload: Mapping[str, Any]) -> list[DmEvent]:
    """The DMs in one webhook delivery. Reads, reactions and anything that isn't a message are ignored.

    Shape: ``{"object": "instagram", "entry": [{"id": <account>, "time": <ms>, "messaging": [{"sender":
    {"id"}, "recipient": {"id"}, "timestamp": <ms>, "message": {"mid", "text"?, "attachments"?,
    "is_echo"?, "is_deleted"?}}]}]}``.
    """
    if payload.get("object") != "instagram":
        return []
    events: list[DmEvent] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, Mapping):
            continue
        for item in entry.get("messaging") or []:
            message = item.get("message") if isinstance(item, Mapping) else None
            if not isinstance(message, Mapping) or not message.get("mid"):
                continue
            stamp = item.get("timestamp") or entry.get("time")
            if not isinstance(stamp, int | float) or stamp <= 0:
                continue
            kind, link = _webhook_kind(message)
            events.append(
                DmEvent(
                    account_id=str(entry.get("id") or ""),
                    sender_id=str((item.get("sender") or {}).get("id") or ""),
                    recipient_id=str((item.get("recipient") or {}).get("id") or ""),
                    message_id=str(message["mid"]),
                    created_at=datetime.fromtimestamp(stamp / 1000 if stamp > 1e11 else stamp, tz=UTC),
                    text=str(message.get("text") or ""),
                    kind=kind,
                    link=link,
                    is_echo=bool(message.get("is_echo")),
                    is_deleted=bool(message.get("is_deleted")),
                )
            )
    return events


def _own(channel: Mapping[str, Any], extra: str = "") -> set[str]:
    ids = own_ids(dict(channel))
    if extra:
        ids.add(extra)
    return ids


async def _insert_message(db: AsyncIOMotorDatabase, row: dict[str, Any]) -> bool:
    """Store a message unless we already hold it, by id or (failing that) by who, what and when."""
    if await db.dm_messages.find_one({"message_id": row["message_id"]}, {"_id": 1}):
        return False
    at = row["created_at"]
    twin = await db.dm_messages.find_one(
        {
            "channel_id": row["channel_id"],
            "peer_id": row["peer_id"],
            "from_me": row["from_me"],
            "text": row["text"],
            "created_at": {"$gte": at - SAME_MESSAGE_WINDOW, "$lte": at + SAME_MESSAGE_WINDOW},
        },
        {"_id": 1},
    )
    if twin:
        return False
    try:
        await db.dm_messages.insert_one(row)
    except DuplicateKeyError:
        return False
    return True


async def _touch_conversation(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    peer_id: str,
    fields: dict[str, Any],
    *,
    history: str | None,
    now: datetime,
) -> None:
    """Create the person's conversation row, or update it. *history* only fills a row that has none."""
    key = {"channel_id": channel_id, "peer_id": peer_id}
    existing = await db.dm_conversations.find_one(key, {"history": 1})
    if existing is None:
        await db.dm_conversations.insert_one({**key, **fields, "history": history, "first_seen_at": now})
        return
    update = dict(fields)
    if existing.get("history") is None and history is not None:
        update["history"] = history
    if update:
        await db.dm_conversations.update_one(key, {"$set": update})


async def record_event(db: AsyncIOMotorDatabase, channel: Mapping[str, Any], event: DmEvent, now: datetime) -> bool:
    """Keep one webhook message. Returns whether it was new."""
    channel_id = str(channel["channel_id"])
    from_me = event.is_echo or event.sender_id in _own(channel, event.account_id)
    peer_id = event.recipient_id if from_me else event.sender_id
    if not peer_id:
        return False
    if event.is_deleted:
        # Unsent on Instagram: keep the row (it happened) but mark it, so it isn't shown as said.
        await db.dm_messages.update_one({"message_id": event.message_id}, {"$set": {"deleted": True}})
        return False
    new_person = await db.dm_conversations.find_one({"channel_id": channel_id, "peer_id": peer_id}, {"_id": 1}) is None
    # After the first full read, someone we've never seen is starting a conversation now: we hold it all.
    history = HISTORY_COMPLETE if new_person and channel.get(BACKFILLED_FIELD) else None
    await _touch_conversation(db, channel_id, peer_id, {}, history=history, now=now)
    return await _insert_message(
        db,
        {
            "message_id": event.message_id,
            "channel_id": channel_id,
            "peer_id": peer_id,
            "from_me": from_me,
            "text": event.text,
            "kind": event.kind,
            "link": event.link,
            "created_at": event.created_at,
            "source": SOURCE_WEBHOOK,
            "logged_at": now,
        },
    )


async def record_webhook(db: AsyncIOMotorDatabase, payload: Mapping[str, Any], now: datetime) -> int:
    """Keep every DM in a webhook delivery; never raises, so comment handling isn't affected."""
    kept = 0
    for event in parse_message_webhook(payload):
        try:
            channel = await channel_for_webhook(db, event.account_id, "")
            if channel is None:
                logger.info("[DmStore] Webhook DM for unknown account %s", event.account_id)
                continue
            kept += await record_event(db, channel, event, now)
        except Exception as exc:  # noqa: BLE001 - a DM we fail to keep is caught by the next catch-up
            logger.warning("[DmStore] Keeping webhook DM %s failed: %s", event.message_id, exc)
    return kept


async def record_sent(
    db: AsyncIOMotorDatabase, channel_id: str, peer_id: str, message_id: str, text: str, now: datetime
) -> None:
    """A DM we just sent from the dashboard, shown at once; its webhook echo then finds it stored."""
    await _touch_conversation(db, channel_id, peer_id, {}, history=None, now=now)
    await _insert_message(
        db,
        {
            "message_id": message_id,
            "channel_id": channel_id,
            "peer_id": peer_id,
            "from_me": True,
            "text": text,
            "kind": "text",
            "link": None,
            "created_at": now,
            "source": SOURCE_SENT,
            "logged_at": now,
        },
    )


def _peer(raw: Mapping[str, Any], own: set[str], own_username: str) -> Mapping[str, Any]:
    participants = [p for p in (raw.get("participants") or {}).get("data") or [] if isinstance(p, Mapping)]
    others = [
        p
        for p in participants
        if str(p.get("id") or "") not in own and str(p.get("username") or "").casefold() != own_username.casefold()
    ]
    return others[0] if others else {}


def api_message_row(
    raw: Mapping[str, Any], channel_id: str, peer_id: str, own: set[str], own_username: str, now: datetime
) -> dict[str, Any] | None:
    """A stored row from one message as the Conversations API returns it."""
    shown = parse_message(raw, own_username or None)
    created = raw.get("created_time")
    if not shown.id or not isinstance(created, str):
        return None
    sender_id = str((raw.get("from") or {}).get("id") or "")
    return {
        "message_id": shown.id,
        "channel_id": channel_id,
        "peer_id": peer_id,
        "from_me": shown.from_me or sender_id in own,
        "text": shown.text or "",
        "kind": shown.kind,
        "link": shown.link,
        "created_at": isoparse(created),
        "source": SOURCE_API,
        "logged_at": now,
    }


async def catch_up(
    db: AsyncIOMotorDatabase,
    channel: Mapping[str, Any],
    reader: Any,
    ig_user_id: str,
    now: datetime,
    *,
    full: bool = False,
    spacing: float | None = None,
) -> dict[str, int]:
    """Bring the stored DMs up to date, reading only what changed.

    Conversations come newest first, so the walk stops at the first one whose ``updated_time`` matches
    what we stored: everything after it is unchanged too. A changed conversation whose newest message
    we already hold (the webhook delivered it) costs nothing more; otherwise its 20 newest messages are
    read and the missing ones kept. *full* walks every conversation, which the first run always does.
    """
    channel_id = str(channel["channel_id"])
    spacing = CALL_SPACING_SECONDS if spacing is None else spacing
    own = _own(channel, ig_user_id)
    own_username = str(channel.get("instagram_username") or "")
    # A walk that didn't finish cleanly leaves this set, and the next one walks everything: stopping early
    # at conversations the broken walk had already updated would skip the ones it never reached.
    full = full or not channel.get(BACKFILLED_FIELD) or bool(channel.get(PENDING_FIELD))
    await db.channels.update_one({"channel_id": channel_id}, {"$set": {PENDING_FIELD: True}})
    stats = {"conversations": 0, "read": 0, "added": 0, "failed": 0}
    after: str | None = None
    while True:
        page = await asyncio.to_thread(reader.list_conversations, ig_user_id, 50, after)
        await asyncio.sleep(spacing)
        stop = False
        for raw in page.get("data") or []:
            peer = _peer(raw, own, own_username)
            peer_id = str(peer.get("id") or "")
            if not peer_id:
                continue
            stats["conversations"] += 1
            updated = str(raw.get("updated_time") or "")
            stored = await db.dm_conversations.find_one({"channel_id": channel_id, "peer_id": peer_id})
            fields = {"conversation_id": str(raw.get("id") or ""), "api_updated_time": updated}
            if peer.get("username"):
                fields["peer_username"] = str(peer["username"])
            if stored and stored.get("api_updated_time") == updated:
                if not full:
                    stop = True
                    break
                continue
            newest = ((raw.get("messages") or {}).get("data") or [{}])[0]
            have_newest = bool(newest.get("id")) and bool(
                await db.dm_messages.find_one({"message_id": str(newest["id"])}, {"_id": 1})
            )
            history: str | None = None
            if not have_newest or (stored or {}).get("history") is None:
                try:
                    messages, more = await asyncio.to_thread(reader.get_conversation_page, fields["conversation_id"])
                except requests.RequestException as exc:
                    logger.warning("[DmStore] Reading conversation with %s (%s) failed: %s", peer_id, channel_id, exc)
                    stats["failed"] += 1
                    continue
                finally:
                    await asyncio.sleep(spacing)
                stats["read"] += 1
                history = HISTORY_PARTIAL if more else HISTORY_COMPLETE
                for message in messages:
                    row = api_message_row(message, channel_id, peer_id, own, own_username, now)
                    if row:
                        stats["added"] += await _insert_message(db, row)
            await _touch_conversation(db, channel_id, peer_id, fields, history=history, now=now)
        after = None if stop else _next_after(page)
        if after is None:
            break
    marks: dict[str, Any] = {SYNCED_FIELD: now, PENDING_FIELD: stats["failed"] > 0}
    if not channel.get(BACKFILLED_FIELD) and not stats["failed"]:
        # Only a clean first read counts: someone whose conversation failed to read would otherwise look new.
        marks[BACKFILLED_FIELD] = now
    await db.channels.update_one({"channel_id": channel_id}, {"$set": marks})
    return stats


def _next_after(page: Mapping[str, Any]) -> str | None:
    paging = page.get("paging") or {}
    if not paging.get("next"):
        return None
    after = (paging.get("cursors") or {}).get("after")
    return str(after) if after else None


async def catch_up_all(db: AsyncIOMotorDatabase, manager: Any, now: datetime) -> dict[str, dict[str, int]]:
    """Catch up every Instagram channel whose token can read messages."""
    from app.services.instagram_tokens import PROVIDER_INSTAGRAM

    results: dict[str, dict[str, int]] = {}
    async for channel in db.channels.find({"platform": "instagram"}):
        channel_id = str(channel["channel_id"])
        try:
            resolved = await manager.get_service_and_user_id(channel_id, PROVIDER_INSTAGRAM)
            if resolved is None:
                continue
            results[channel_id] = await catch_up(db, channel, resolved[0], resolved[1], now)
        except requests.RequestException as exc:
            # Mostly a token without the messages permission; nothing to keep for that channel.
            logger.info("[DmStore] Catch-up of %s skipped: %s", channel_id, exc)
        except Exception as exc:  # noqa: BLE001 - one channel mustn't stop the rest
            logger.warning("[DmStore] Catch-up of %s failed: %s", channel_id, exc)
    return results


# The webhook carries DMs live; the catch-up only fills gaps.
CATCH_UP_EVERY_SECONDS = 3 * 3600
FIRST_CATCH_UP_DELAY_SECONDS = 180


async def run_dm_catch_up(db: AsyncIOMotorDatabase, manager: Any) -> None:
    """Loop forever: the first pass is the backfill, later ones pick up anything the webhook missed."""
    from app.timezone import now_ist

    await asyncio.sleep(FIRST_CATCH_UP_DELAY_SECONDS)
    while True:
        results = await catch_up_all(db, manager, now_ist())
        added = {cid: r["added"] for cid, r in results.items() if r["added"]}
        logger.info("[DmStore] Catch-up done for %d channels; new messages: %s", len(results), added or "none")
        await asyncio.sleep(CATCH_UP_EVERY_SECONDS)
