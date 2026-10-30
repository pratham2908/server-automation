"""Where AI comment replies keep their per-channel config and their queue of comments to answer.

- The config sits on the channel document (``comment_ai_reply``).
- Every comment worth an answer becomes one ``comment_ai_reply_queue`` document keyed by comment id,
  which is what stops the webhook delivering the same comment twice from answering it twice.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.services.comment_ai_reply import (
    SKIP_ALREADY_ANSWERED,
    SKIP_OWN_DM_REPLY,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENDING,
    STATUS_SKIPPED,
    AiReplyConfig,
    StoredAiReply,
    reply_delay,
    skip_reason,
    stored_config,
)
from app.services.comment_dm import CommentEvent
from app.services.comment_dm_store import own_ids

CONFIG_FIELD = "comment_ai_reply"
QUEUED = "queued"
DUPLICATE = "duplicate"
OFF = "off"


async def load_config(db: AsyncIOMotorDatabase, channel_id: str) -> StoredAiReply | None:
    channel = await db.channels.find_one({"channel_id": channel_id}, {CONFIG_FIELD: 1})
    doc = (channel or {}).get(CONFIG_FIELD)
    return StoredAiReply.model_validate(doc) if doc else None


async def save_config(db: AsyncIOMotorDatabase, channel_id: str, config: AiReplyConfig, now: datetime) -> StoredAiReply:
    saved = stored_config(config, await load_config(db, channel_id), now)
    await db.channels.update_one({"channel_id": channel_id}, {"$set": {CONFIG_FIELD: saved.model_dump()}})
    return saved


async def enqueue(
    db: AsyncIOMotorDatabase,
    channel: dict[str, Any],
    event: CommentEvent,
    *,
    dm_replies_publicly: bool,
    now: datetime,
    rng: random.Random,
) -> str:
    """Queue an AI reply for this comment if the channel has them on. Returns what happened, for logs.

    *dm_replies_publicly* is whether comment-to-DM queued this same comment with a public reply of its
    own: two replies under one comment would read as a bot.
    """
    channel_id = str(channel["channel_id"])
    config = await load_config(db, channel_id)
    if config is None or not config.enabled:
        return OFF
    if await db.comment_ai_reply_queue.find_one({"comment_id": event.comment_id}, {"_id": 1}):
        return DUPLICATE
    reason = skip_reason(
        event,
        own_ids=own_ids(channel),
        own_username=str(channel.get("instagram_username") or ""),
        active_since=config.enabled_at,
        now=now,
        reply_in_threads=config.reply_in_threads,
    )
    if reason is None and dm_replies_publicly:
        reason = SKIP_OWN_DM_REPLY
    if reason is None and await db.comment_replies.find_one({"comment_id": event.comment_id}, {"_id": 1}):
        reason = SKIP_ALREADY_ANSWERED
    doc = {
        "comment_id": event.comment_id,
        "channel_id": channel_id,
        "media_id": event.media_id,
        "username": event.username,
        "text": event.text,
        "commented_at": event.created_at,
        "status": STATUS_SKIPPED if reason else STATUS_PENDING,
        "skip_reason": reason,
        "send_after": now + reply_delay(config, rng),
        "created_at": now,
        "updated_at": now,
    }
    try:
        await db.comment_ai_reply_queue.insert_one(doc)
    except DuplicateKeyError:
        # The webhook delivered the comment twice at once; the unique index let exactly one through.
        return DUPLICATE
    return reason or QUEUED


async def claim_due(db: AsyncIOMotorDatabase, now: datetime) -> dict[str, Any] | None:
    """Take the oldest reply that is due, marking it in flight so no other pass answers it too."""
    doc = await db.comment_ai_reply_queue.find_one_and_update(
        {"status": STATUS_PENDING, "send_after": {"$lte": now}},
        {"$set": {"status": STATUS_SENDING, "claimed_at": now, "updated_at": now}},
        sort=[("send_after", 1)],
        return_document=ReturnDocument.AFTER,
    )
    return dict(doc) if doc else None


async def update_item(db: AsyncIOMotorDatabase, comment_id: str, fields: dict[str, Any], now: datetime) -> None:
    await db.comment_ai_reply_queue.update_one({"comment_id": comment_id}, {"$set": {**fields, "updated_at": now}})


async def fail_interrupted(db: AsyncIOMotorDatabase, stale_before: datetime, now: datetime) -> int:
    """Sends cut off by a restart are failed, not retried: the reply may already be posted, and two
    replies under one comment is worse than none."""
    result = await db.comment_ai_reply_queue.update_many(
        {"status": STATUS_SENDING, "claimed_at": {"$lte": stale_before}},
        {"$set": {"status": STATUS_FAILED, "error": "interrupted while sending; not retried", "updated_at": now}},
    )
    return int(result.modified_count)
