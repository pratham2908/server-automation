"""Answers queued Instagram comments with an AI reply, once each one's delay has passed.

Comments arrive through Meta's webhook (``routers/meta_webhooks.py``), which queues them here after the
channel's delay. A channel in review mode gets a draft in the approval queue instead of a posted reply.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import requests
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.comment_ai_reply import STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED
from app.services.comment_ai_reply_store import claim_due, fail_interrupted, load_config, update_item
from app.services.comment_reply_review import MODE_REVIEW, build_pending_row, is_reviewable, normalise_mode
from app.services.comment_reply_text import video_context
from app.services.error_reporting import get_error_service
from app.services.gemini import GeminiService
from app.services.instagram import graph_error_message
from app.services.instagram_tokens import PREFER_FOR_REPLYING
from app.timezone import now_ist

logger = get_logger(__name__)

TICK_SECONDS = 10
MAX_PER_TICK = 20
# Longer than one reply takes, so only a reply cut off by a restart is ever this old.
STUCK_AFTER = timedelta(minutes=10)


def _answer_row(
    channel_id: str, item: dict[str, Any], video: dict[str, Any], sentiment: str, status: str, now: datetime
) -> dict[str, Any]:
    """A ``comment_replies`` document, so webhook replies show in the same history as the cycle's and
    the cycle never answers a comment twice."""
    return {
        "channel_id": channel_id,
        "video_id": video.get("video_id", ""),
        "video_title": video.get("title", ""),
        "video_url": "",
        "platform": "instagram",
        "comment_id": item["comment_id"],
        "comment_text": item.get("text", ""),
        "comment_author": item.get("username", ""),
        "comment_url": "",
        "sentiment": sentiment,
        "status": status,
        "replied_at": now,
    }


async def answer_one(
    db: AsyncIOMotorDatabase, manager: Any, gemini: GeminiService, item: dict[str, Any], now: datetime
) -> str:
    """Classify the comment, then post, draft or skip. Returns the status the queue item ends in."""
    comment_id = str(item["comment_id"])
    channel_id = str(item["channel_id"])
    config = await load_config(db, channel_id)
    channel = await db.channels.find_one({"channel_id": channel_id}, {"comment_reply_mode": 1})
    if config is None or not config.enabled or channel is None:
        await update_item(db, comment_id, {"status": STATUS_SKIPPED, "skip_reason": "switched off"}, now)
        return STATUS_SKIPPED
    if await db.comment_replies.find_one({"comment_id": comment_id}, {"_id": 1}):
        await update_item(db, comment_id, {"status": STATUS_SKIPPED, "skip_reason": "already answered"}, now)
        return STATUS_SKIPPED
    if not str(item.get("text") or "").strip():
        await update_item(db, comment_id, {"status": STATUS_SKIPPED, "skip_reason": "no text"}, now)
        return STATUS_SKIPPED

    video = await db.videos.find_one({"channel_id": channel_id, "instagram_media_id": item["media_id"]}) or {}
    context = video_context(video) if video else ""
    review = normalise_mode(channel.get("comment_reply_mode")) == MODE_REVIEW
    comment = {"comment_id": comment_id, "text": item["text"], "author": item.get("username", "")}

    try:
        results = await gemini.classify_comment_sentiment([comment], video_context=context)
        sentiment = next(
            (r.get("sentiment", "neutral") for r in results if r.get("comment_id") == comment_id), "neutral"
        )
        if sentiment != "positive" and not (review and is_reviewable(sentiment)):
            # Same bookkeeping as the cycle's skips, so the comment isn't read again.
            await db.comment_replies.insert_one(
                _answer_row(channel_id, item, video, sentiment, f"skipped_{sentiment}", now)
            )
            await update_item(db, comment_id, {"status": STATUS_SKIPPED, "skip_reason": f"{sentiment} comment"}, now)
            return STATUS_SKIPPED
        reply = await gemini.generate_comment_reply(
            comment_text=item["text"],
            video_title=video.get("title", ""),
            platform="instagram",
            sentiment=sentiment,
            video_context=context,
            instructions=config.instructions,
        )
        if not reply:
            # No canned fallback: it would be a subscribe pitch under a comment that may not be praise.
            await update_item(db, comment_id, {"status": STATUS_FAILED, "error": "the AI returned no reply"}, now)
            return STATUS_FAILED
        if review:
            row = build_pending_row(
                channel_id=channel_id,
                platform="instagram",
                video=video,
                comment=comment,
                sentiment=sentiment,
                suggested_reply=reply,
            )
            await db.comment_replies.insert_one(row)
            await update_item(db, comment_id, {"status": STATUS_DONE, "reply_text": reply, "drafted": True}, now)
            return STATUS_DONE
        service = await manager.get_service(channel_id, PREFER_FOR_REPLYING)
        if service is None:
            await update_item(db, comment_id, {"status": STATUS_FAILED, "error": "channel has no Instagram token"}, now)
            return STATUS_FAILED
        reply_id = await asyncio.to_thread(service.reply_to_comment, comment_id, reply)
    except requests.RequestException as exc:
        error = graph_error_message(exc)
        logger.warning("[AiReply] Replying to comment %s on %s failed: %s", comment_id, channel_id, error)
        await update_item(db, comment_id, {"status": STATUS_FAILED, "error": error}, now)
        return STATUS_FAILED

    await db.comment_replies.insert_one(
        {**_answer_row(channel_id, item, video, "positive", "replied", now), "reply_text": reply, "reply_id": reply_id}
    )
    await update_item(db, comment_id, {"status": STATUS_DONE, "reply_text": reply, "reply_id": reply_id}, now)
    logger.info("[AiReply] Replied to comment %s on %s (@%s)", comment_id, channel_id, item.get("username"))
    return STATUS_DONE


async def run_comment_ai_reply_worker(db: AsyncIOMotorDatabase, manager: Any, gemini: GeminiService) -> None:
    """Long-running loop: answers whatever has come due, every few seconds."""
    now = now_ist()
    failed = await fail_interrupted(db, now - STUCK_AFTER, now)
    if failed:
        logger.warning("[AiReply] %d reply(ies) were cut off by a restart and marked failed", failed)
    logger.info("[AiReply] Worker started (tick %ds)", TICK_SECONDS)
    while True:
        try:
            for _ in range(MAX_PER_TICK):
                now = now_ist()
                item = await claim_due(db, now)
                if item is None:
                    break
                try:
                    await answer_one(db, manager, gemini, item, now)
                except Exception as exc:
                    # One bad comment must not stop the rest; it is failed, not retried, so it can't answer twice.
                    logger.exception("[AiReply] Comment %s failed", item.get("comment_id"))
                    await update_item(
                        db, str(item["comment_id"]), {"status": STATUS_FAILED, "error": str(exc)[:300]}, now
                    )
        except asyncio.CancelledError:
            logger.info("[AiReply] Worker shutting down")
            break
        except Exception as exc:
            logger.exception("[AiReply] Worker pass failed")
            await get_error_service(db).log_error(
                feature="AI Comment Reply Worker", message="AI comment reply pass failed", exception=exc
            )
        await asyncio.sleep(TICK_SECONDS)
