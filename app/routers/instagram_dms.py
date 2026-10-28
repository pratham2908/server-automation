"""Read an Instagram account's DMs, only when asked.

Nothing here runs on a schedule or stores messages: each request goes to Instagram and the
answer is returned as it stands. Needs a token with ``instagram_manage_messages`` — the shared
Facebook token on most channels doesn't have it, which comes back as a clear 403.
"""

from __future__ import annotations

import asyncio

import requests
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key
from app.services.instagram import InstagramService, graph_error_message
from app.services.instagram_dm_inbox import (
    DmConversationOut,
    DmMessageOut,
    next_cursor,
    parse_conversation,
    parse_thread,
    reply_window_ends_at,
)
from app.services.instagram_tokens import PROVIDER_INSTAGRAM
from app.timezone import now_ist

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/instagram/dms",
    tags=["instagram-dms"],
    dependencies=[Depends(verify_api_key)],
)

# Conversation ids are long opaque strings; reject anything that isn't one before it reaches Meta.
CONVERSATION_ID = Path(pattern=r"^[A-Za-z0-9_\-]{8,400}$")


class ConversationsOut(BaseModel):
    channel_id: str
    conversations: list[DmConversationOut]
    # Pass back as ``after`` for the next page; None on the last one.
    next_cursor: str | None = None


class ThreadOut(BaseModel):
    channel_id: str
    conversation_id: str
    messages: list[DmMessageOut]
    # When we may no longer message this person; None once that has passed.
    reply_window_ends_at: str | None = None


class SendIn(BaseModel):
    recipient_id: str = Field(pattern=r"^[0-9]{5,32}$")
    # Instagram caps a text DM at 1,000 characters.
    text: str = Field(min_length=1, max_length=1000)


class SendOut(BaseModel):
    channel_id: str
    recipient_id: str
    message_id: str


async def _service(channel_id: str, db: AsyncIOMotorDatabase) -> tuple[InstagramService, str, str | None]:
    """The channel's Instagram client, the id to address, and its own handle."""
    import app.main as main_mod

    if not await db.channels.find_one({"channel_id": channel_id}, {"_id": 1}):
        raise HTTPException(404, "Channel not found")
    manager = main_mod.instagram_service_manager
    resolved = await manager.get_service_and_user_id(channel_id, PROVIDER_INSTAGRAM) if manager else None
    if resolved is None:
        raise HTTPException(422, "This channel has no Instagram token")
    service, ig_user_id = resolved
    try:
        own = await asyncio.to_thread(service.own_username, ig_user_id)
    except requests.RequestException as exc:
        raise _refused(exc) from exc
    return service, ig_user_id, own


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


@router.get("", response_model=ConversationsOut)
async def list_dms(
    channel_id: str,
    limit: int = Query(20, ge=1, le=50),
    after: str | None = Query(None, max_length=400, description="Cursor from the previous page"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ConversationsOut:
    """Conversations, most recently active first, each with its newest message."""
    service, ig_user_id, own = await _service(channel_id, db)
    try:
        page = await asyncio.to_thread(service.list_conversations, ig_user_id, limit, after)
    except requests.RequestException as exc:
        raise _refused(exc) from exc
    return ConversationsOut(
        channel_id=channel_id,
        conversations=[parse_conversation(raw, own) for raw in page.get("data", [])],
        next_cursor=next_cursor(page),
    )


@router.get("/{conversation_id}", response_model=ThreadOut)
async def get_dm_thread(
    channel_id: str,
    conversation_id: str = CONVERSATION_ID,
    limit: int = Query(50, ge=1, le=100),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ThreadOut:
    """The newest *limit* messages of one conversation, oldest first."""
    service, _, own = await _service(channel_id, db)
    try:
        raw = await asyncio.to_thread(service.get_conversation_messages, conversation_id, limit)
    except requests.RequestException as exc:
        raise _refused(exc) from exc
    messages = parse_thread(raw, own)
    return ThreadOut(
        channel_id=channel_id,
        conversation_id=conversation_id,
        messages=messages,
        reply_window_ends_at=reply_window_ends_at(messages, now_ist()),
    )


@router.post("/send", response_model=SendOut)
async def send_dm(channel_id: str, body: SendIn, db: AsyncIOMotorDatabase = Depends(get_db)) -> SendOut:
    """Send one text DM, only because this was called — nothing replies on its own.

    Instagram decides whether it may go: it must be within 24 hours of the person's last message.
    A refusal comes back with Instagram's own reason.
    """
    service, ig_user_id, _ = await _service(channel_id, db)
    try:
        message_id = await asyncio.to_thread(service.send_dm, ig_user_id, body.recipient_id, body.text)
    except requests.RequestException as exc:
        raise _refused(exc) from exc
    return SendOut(channel_id=channel_id, recipient_id=body.recipient_id, message_id=message_id)
