"""Replies owed for taps, sent by the worker rather than inside the webhook.

Meta wants a webhook answered within seconds and redelivers when it isn't, and a reply may first need
a follow check (one more Instagram call). So a tap is written here and the comment-DM worker's tick
sends it moments later. One row per tap, keyed by the tap's id, so a redelivered tap sends nothing twice.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import requests
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import ValidationError
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.logger import get_logger
from app.services import dm_replies, dm_store
from app.services.comment_dm import is_rate_limited, retry_at
from app.services.comment_dm_store import channel_for_webhook, own_ids
from app.services.dm_content import DmMessage, following_gate, payload_reply_id
from app.services.dm_sender import deliver, follows_us, r2_signer
from app.services.dm_taps import DmTap, parse_taps
from app.services.instagram import graph_error_code, graph_error_message
from app.services.instagram_tokens import PROVIDER_INSTAGRAM

logger = get_logger(__name__)

STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"

MAX_SENDS_PER_TICK = 20
STUCK_AFTER = timedelta(minutes=10)
# Instagram's window after their last message. A tap older than this can no longer be answered.
REPLY_WINDOW = timedelta(hours=24)

QUEUED = "queued"
DUPLICATE = "duplicate"
NOT_OURS = "not one of our replies"
UNKNOWN_REPLY = "reply not found"


async def enqueue(
    db: AsyncIOMotorDatabase,
    *,
    key: str,
    channel_id: str,
    recipient_id: str,
    message: DmMessage,
    check_follow: bool,
    reason: str,
    now: datetime,
) -> bool:
    """Owe *recipient_id* this message. False when *key* was already queued."""
    if await db.dm_outbox.find_one({"key": key}, {"_id": 1}):
        return False
    try:
        await db.dm_outbox.insert_one(
            {
                "key": key,
                "channel_id": channel_id,
                "recipient_id": recipient_id,
                "message": message.model_dump(mode="json"),
                "check_follow": check_follow,
                "reason": reason,
                "status": STATUS_PENDING,
                "send_after": now,
                "attempts": 0,
                "created_at": now,
                "updated_at": now,
            }
        )
    except DuplicateKeyError:
        return False
    return True


async def handle_tap(db: AsyncIOMotorDatabase, channel: Mapping[str, Any], tap: DmTap, now: datetime) -> str:
    """Queue the reply a tap asks for. Returns what happened, for logs and tests."""
    channel_id = str(channel["channel_id"])
    if tap.sender_id in own_ids(dict(channel)) or tap.sender_id == tap.account_id:
        return NOT_OURS
    if tap.kind == "postback":
        # A chip is a message and the inbox keeps it already; a postback would otherwise leave no trace.
        await dm_store.record_tap(db, channel, tap.sender_id, tap.tap_id, tap.title, tap.at, now)
    rid = payload_reply_id(tap.payload)
    if rid is None:
        return NOT_OURS
    reply = await dm_replies.find(db, channel_id, rid)
    if reply is None:
        logger.warning("[DmOutbox] Tap %s on %s names unknown reply %s", tap.tap_id, channel_id, rid)
        return UNKNOWN_REPLY
    queued = await enqueue(
        db,
        key=tap.tap_id,
        channel_id=channel_id,
        recipient_id=tap.sender_id,
        message=reply,
        check_follow=bool(reply.followers_only_text),
        reason=tap.kind,
        now=now,
    )
    return QUEUED if queued else DUPLICATE


async def record_webhook(db: AsyncIOMotorDatabase, payload: Mapping[str, Any], now: datetime) -> int:
    """Queue a reply for every tap in a delivery; never raises, so comments and DMs aren't affected."""
    queued = 0
    for tap in parse_taps(payload):
        try:
            channel = await channel_for_webhook(db, tap.account_id, "")
            if channel is None:
                logger.info("[DmOutbox] Tap for unknown account %s", tap.account_id)
                continue
            outcome = await handle_tap(db, channel, tap, now)
            logger.info("[DmOutbox] Tap %r by %s on %s: %s", tap.title, tap.sender_id, channel["channel_id"], outcome)
            queued += outcome == QUEUED
        except Exception as exc:  # noqa: BLE001 - one bad tap must not lose the rest of the delivery
            logger.warning("[DmOutbox] Handling tap %s failed: %s", tap.tap_id, exc)
    return queued


async def claim_due(db: AsyncIOMotorDatabase, now: datetime) -> dict[str, Any] | None:
    doc = await db.dm_outbox.find_one_and_update(
        {"status": STATUS_PENDING, "send_after": {"$lte": now}},
        {"$set": {"status": STATUS_SENDING, "claimed_at": now, "updated_at": now}},
        sort=[("send_after", 1)],
        return_document=ReturnDocument.AFTER,
    )
    return dict(doc) if doc else None


async def _update(db: AsyncIOMotorDatabase, key: str, fields: dict[str, Any], now: datetime) -> None:
    await db.dm_outbox.update_one({"key": key}, {"$set": {**fields, "updated_at": now}})


async def send_one(db: AsyncIOMotorDatabase, manager: Any, item: dict[str, Any], now: datetime) -> str:
    """Send one owed reply. Returns the status it ends in."""
    key = str(item["key"])
    channel_id = str(item["channel_id"])
    recipient = str(item["recipient_id"])
    channel = await db.channels.find_one({"channel_id": channel_id})
    resolved = await manager.get_service_and_user_id(channel_id, prefer=PROVIDER_INSTAGRAM) if channel else None
    if channel is None or resolved is None:
        await _update(db, key, {"status": STATUS_FAILED, "error": "channel has no Instagram token"}, now)
        return STATUS_FAILED
    service, ig_user_id = resolved
    try:
        message = DmMessage.model_validate(item["message"])
    except ValidationError as exc:
        await _update(db, key, {"status": STATUS_FAILED, "error": f"stored reply is invalid: {exc}"[:500]}, now)
        return STATUS_FAILED
    fields: dict[str, Any] = {}
    if item.get("check_follow"):
        follows = await follows_us(service, recipient)
        # Unknown counts as following: the check exists to nudge, and a follower told to follow is worse
        # than a non-follower who got the link anyway.
        fields["follows"] = follows
        if follows is False:
            message = following_gate(message, channel_id)
    attempts = int(item.get("attempts", 0)) + 1
    try:
        ids = await deliver(db, service, ig_user_id, channel, recipient, message, now, r2_signer())
    except requests.RequestException as exc:
        error = graph_error_message(exc)
        again = retry_at(attempts, now) if is_rate_limited(graph_error_code(exc)) else None
        if again is not None:
            await _update(db, key, {"status": STATUS_PENDING, "send_after": again, "attempts": attempts}, now)
            return STATUS_PENDING
        logger.warning("[DmOutbox] Reply %s to %s on %s failed: %s", key, recipient, channel_id, error)
        await _update(db, key, {"status": STATUS_FAILED, "attempts": attempts, "error": error, **fields}, now)
        return STATUS_FAILED
    except ValueError as exc:
        await _update(db, key, {"status": STATUS_FAILED, "attempts": attempts, "error": str(exc), **fields}, now)
        return STATUS_FAILED
    gated = fields.get("follows") is False
    done = {"status": STATUS_SENT, "attempts": attempts, "sent_at": now, "message_ids": ids, "gated": gated}
    await _update(db, key, {**done, "error": None, **fields}, now)
    return STATUS_SENT


async def fail_interrupted(db: AsyncIOMotorDatabase, now: datetime) -> int:
    """A send cut off by a restart may have gone out; failing it beats sending it twice."""
    result = await db.dm_outbox.update_many(
        {"status": STATUS_SENDING, "claimed_at": {"$lte": now - STUCK_AFTER}},
        {"$set": {"status": STATUS_FAILED, "error": "interrupted while sending; not retried", "updated_at": now}},
    )
    return int(result.modified_count)


async def send_due(db: AsyncIOMotorDatabase, manager: Any, now_fn: Any) -> int:
    """Send what's owed, up to a tick's worth. *now_fn* gives the time per send."""
    sent = 0
    for _ in range(MAX_SENDS_PER_TICK):
        now = now_fn()
        item = await claim_due(db, now)
        if item is None:
            break
        sent += await send_one(db, manager, item, now) == STATUS_SENT
    return sent
