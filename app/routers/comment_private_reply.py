"""Send one private reply to one Instagram comment, only because this was called.

A private reply is the DM Instagram lets an account send to someone who commented on its post: one per
comment, within 7 days of it. The comment-DM worker sends these on its own for reels with a rule; this
route sends one by hand, for comments no rule covered (people who asked for something in the comments).

The message is the same ``DmMessage`` ``/instagram/dms/send`` takes (text with buttons, or cards), built
with the same builder the worker uses. It is recorded in ``comment_dm_events`` the way the worker records
its own, so the worker never sends a second DM for that comment. Unlike the worker there is no automatic
plain-text fallback when Instagram refuses a template: the refusal comes back with Instagram's reason and
nothing is spent (Instagram only counts a private reply that went), so the caller decides what to resend.
"""

from __future__ import annotations

import asyncio
from typing import Any

import requests
from fastapi import APIRouter, Depends, HTTPException, Path
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger
from app.services import dm_replies
from app.services.comment_dm import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_SKIPPED,
    CommentEvent,
    person_key,
)
from app.services.dm_content import DmMessage, build_messages
from app.services.dm_sender import context_for, r2_signer
from app.services.instagram import graph_error_message
from app.services.instagram_tokens import PROVIDER_INSTAGRAM
from app.timezone import now_ist

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/instagram/comments",
    tags=["instagram-private-reply"],
    dependencies=[Depends(verify_api_key)],
)

COMMENT_ID = Path(pattern=r"^[0-9]{5,32}$")
SOURCE_MANUAL = "manual"
# What the stored event goes back to when a send that took over a queued one is refused.
_RESTORED = ("status", "error", "attempts", "claimed_at")


class PrivateReplyOut(BaseModel):
    channel_id: str
    comment_id: str
    message_id: str
    # Who it went to, when the comment is one we've seen; None otherwise.
    username: str | None = None
    # "cards", "buttons" or "text": what Instagram accepted.
    dm_format: str


def _format(message: DmMessage) -> str:
    if message.cards:
        return "cards"
    return "buttons" if message.buttons or message.quick_replies else "text"


def _refused(exc: requests.RequestException) -> HTTPException:
    message = graph_error_message(exc)
    lowered = message.casefold()
    if "window" in lowered or "already" in lowered or "expired" in lowered:
        return HTTPException(
            409,
            "Instagram allows one private reply per comment, within 7 days of it, and refused this one: "
            f"{message[:300]}",
        )
    return HTTPException(502, f"Instagram refused: {message[:300]}")


async def _service(channel_id: str) -> tuple[Any, str]:
    import app.main as main_mod

    manager = main_mod.instagram_service_manager
    # Instagram Login sends a private reply with the comment permission alone (see the worker).
    resolved = await manager.get_service_and_user_id(channel_id, prefer=PROVIDER_INSTAGRAM) if manager else None
    if resolved is None:
        raise HTTPException(422, "This channel has no Instagram token")
    return resolved


async def _claim(
    db: AsyncIOMotorDatabase, channel_id: str, comment_id: str, now: Any
) -> tuple[dict[str, Any] | None, bool]:
    """Mark the comment's event as being sent. Returns the event as it was before, and whether we made it.

    Raises 409 when a DM for this comment already went, or is going out right now.
    """
    existing = await db.comment_dm_events.find_one({"comment_id": comment_id})
    if existing is None:
        seen = await db.comment_log.find_one({"comment_id": comment_id}) or {}
        username = str(seen.get("username") or "")
        from_id = str(seen.get("from_id") or "")
        event = CommentEvent(comment_id=comment_id, media_id="", username=username, from_id=from_id)
        person = person_key(event) if username or from_id else None
        doc = {
            "comment_id": comment_id,
            "channel_id": channel_id,
            "media_id": seen.get("media_id"),
            "person": person,
            "username": username,
            "from_id": from_id,
            "text": seen.get("text"),
            "commented_at": seen.get("commented_at"),
            "source": SOURCE_MANUAL,
            "status": STATUS_SENDING,
            "skip_reason": None,
            "attempts": 0,
            "claimed_at": now,
            "created_at": now,
            "updated_at": now,
        }
        try:
            await db.comment_dm_events.insert_one(doc)
        except DuplicateKeyError:
            # The webhook queued it a moment ago: take that one over instead.
            return await _claim(db, channel_id, comment_id, now)
        return None, True
    if existing.get("channel_id") not in (None, channel_id):
        raise HTTPException(409, "This comment belongs to another channel")
    if existing.get("status") == STATUS_SENT:
        raise HTTPException(409, "A private reply already went to this comment; Instagram allows only one")
    # Queued, failed or skipped by the worker: taken over atomically, so the worker can't send it as well.
    taken = await db.comment_dm_events.find_one_and_update(
        {"comment_id": comment_id, "status": {"$in": [STATUS_PENDING, STATUS_FAILED, STATUS_SKIPPED]}},
        {"$set": {"status": STATUS_SENDING, "claimed_at": now, "updated_at": now}},
    )
    if taken is None:
        raise HTTPException(409, "A DM for this comment is being sent right now")
    return dict(existing), False


@router.post("/{comment_id}/private-reply", response_model=PrivateReplyOut)
async def send_private_reply(
    channel_id: str,
    body: DmMessage,
    comment_id: str = COMMENT_ID,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> PrivateReplyOut:
    """DM the person who wrote *comment_id*, once. Returns Instagram's message id, or its refusal.

    One message only: text (with up to 3 buttons) or cards, or media alone. Instagram allows one private
    reply per comment, so media with text would need two and is refused here before anything is sent.
    """
    channel = await db.channels.find_one({"channel_id": channel_id})
    if channel is None:
        raise HTTPException(404, "Channel not found")
    try:
        parts = build_messages(body, context_for(channel, body, r2_signer()))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if len(parts) != 1:
        raise HTTPException(
            422, "A private reply is a single message: send media alone, or text/buttons/cards without media"
        )
    service, ig_user_id = await _service(channel_id)
    now = now_ist()
    before, created = await _claim(db, channel_id, comment_id, now)
    # Before sending, so a tap a second later finds the reply its button names.
    await dm_replies.register_message(db, channel_id, body, now)
    try:
        message_id = await asyncio.to_thread(service.send_private_reply, ig_user_id, comment_id, parts[0])
    except requests.RequestException as exc:
        error = graph_error_message(exc)
        logger.warning("[PrivateReply] Comment %s on %s refused: %s", comment_id, channel_id, error)
        if created:
            # Nothing went out: leave no trace, so the comment is exactly as it was.
            await db.comment_dm_events.delete_one({"comment_id": comment_id, "source": SOURCE_MANUAL})
        else:
            restored = {k: before.get(k) for k in _RESTORED} if before else {"status": STATUS_FAILED}
            await db.comment_dm_events.update_one(
                {"comment_id": comment_id}, {"$set": {**restored, "updated_at": now_ist()}}
            )
        raise _refused(exc) from exc
    dm_format = _format(body)
    fields = {
        "status": STATUS_SENT,
        "sent_at": now,
        "dm_message_id": message_id,
        "dm_format": dm_format,
        "dm_text": body.text,
        "buttons": [b.model_dump(mode="json") for b in body.buttons],
        "cards": [c.model_dump(mode="json") for c in body.cards],
        "quick_replies": [q.model_dump(mode="json") for q in body.quick_replies],
        "manual": True,
        "error": None,
        "attempts": int((before or {}).get("attempts") or 0) + 1,
        "updated_at": now_ist(),
    }
    await db.comment_dm_events.update_one({"comment_id": comment_id}, {"$set": fields})
    stored = await db.comment_dm_events.find_one({"comment_id": comment_id}, {"username": 1}) or {}
    logger.info("[PrivateReply] Sent for comment %s on %s (%s)", comment_id, channel_id, dm_format)
    return PrivateReplyOut(
        channel_id=channel_id,
        comment_id=comment_id,
        message_id=message_id,
        username=stored.get("username") or None,
        dm_format=dm_format,
    )
