"""The comment-to-DM loop: send DMs that are due, and poll for comments the webhook may have missed.

Meta's webhook is the main way comments arrive (see ``routers/meta_webhooks.py``), but its delivery is
at-least-once and not guaranteed, so every few minutes this also reads the newest comments on reels
with a rule switched on. Reading needs a Facebook Login token — Instagram Login gets no comments back
for this app — so a channel without one relies on the webhook alone. Duplicates are harmless: the
queue is keyed by comment id.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import Any

import requests
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.comment_dm import (
    HOURLY_CAP,
    PRIVATE_REPLY_WINDOW,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENT,
    CommentEvent,
    is_rate_limited,
    retry_at,
)
from app.services.comment_dm_store import (
    DEFAULT_FIELD,
    QUEUED,
    channels_with_rules,
    claim_due,
    enqueue,
    fail_interrupted,
    list_reel_rules,
    sent_since,
    update_event,
)
from app.services.error_reporting import get_error_service
from app.services.instagram import graph_error_message
from app.services.instagram_content_analysis import parse_published_at
from app.services.instagram_tokens import PROVIDER_FACEBOOK, PROVIDER_INSTAGRAM
from app.timezone import now_ist

logger = get_logger(__name__)

TICK_SECONDS = 10
POLL_INTERVAL = timedelta(minutes=5)
MAX_SENDS_PER_TICK = 20
# Longer than any one send takes, so only a send cut off by a restart is ever this old.
STUCK_AFTER = timedelta(minutes=10)
CAP_PAUSE = timedelta(minutes=5)
# Reels this recent are polled under a channel default; older ones can't take a private reply anyway.
POLL_REEL_LIMIT = 30


def _graph_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        code = (response.json().get("error") or {}).get("code")
    except ValueError:
        # Not JSON (a proxy error page): no Graph code to classify by.
        return None
    return int(code) if isinstance(code, int) else None


async def send_one(db: AsyncIOMotorDatabase, manager: Any, event: dict[str, Any], now: datetime) -> str:
    """DM the commenter, then reply to the comment. Returns the status the event ends in."""
    comment_id = str(event["comment_id"])
    channel_id = str(event["channel_id"])
    if await sent_since(db, channel_id, now - timedelta(hours=1)) >= HOURLY_CAP:
        await update_event(db, comment_id, {"status": STATUS_PENDING, "send_after": now + CAP_PAUSE}, now)
        return STATUS_PENDING
    # Instagram Login can send a private reply with the comment permission alone; Facebook Login also
    # needs instagram_manage_messages, which the shared token lacks — so prefer Instagram Login.
    resolved = await manager.get_service_and_user_id(channel_id, prefer=PROVIDER_INSTAGRAM)
    if resolved is None:
        await update_event(db, comment_id, {"status": STATUS_FAILED, "error": "channel has no Instagram token"}, now)
        return STATUS_FAILED
    service, ig_user_id = resolved
    attempts = int(event.get("attempts", 0)) + 1
    try:
        message_id = await asyncio.to_thread(service.send_private_reply, ig_user_id, comment_id, event["dm_text"])
    except requests.RequestException as exc:
        error = graph_error_message(exc)
        again = retry_at(attempts, now) if is_rate_limited(_graph_code(exc)) else None
        if again is not None:
            retry = {"status": STATUS_PENDING, "send_after": again, "attempts": attempts, "error": error}
            await update_event(db, comment_id, retry, now)
            return STATUS_PENDING
        logger.warning("[CommentDM] DM for comment %s on %s failed: %s", comment_id, channel_id, error)
        await update_event(db, comment_id, {"status": STATUS_FAILED, "attempts": attempts, "error": error}, now)
        return STATUS_FAILED

    fields: dict[str, Any] = {
        "status": STATUS_SENT,
        "attempts": attempts,
        "dm_message_id": message_id,
        "sent_at": now,
        "error": None,
    }
    reply_text = event.get("reply_text")
    if reply_text:
        # The DM is what matters; a failed public reply is recorded but doesn't undo it.
        try:
            fields["reply_comment_id"] = await asyncio.to_thread(service.reply_to_comment, comment_id, reply_text)
        except requests.RequestException as exc:
            fields["reply_error"] = graph_error_message(exc)
    await update_event(db, comment_id, fields, now)
    logger.info("[CommentDM] DM sent for comment %s on %s (@%s)", comment_id, channel_id, event.get("username"))
    return STATUS_SENT


def _event_from_graph(item: dict[str, Any], media_id: str) -> CommentEvent:
    sender = item.get("from") or {}
    return CommentEvent(
        comment_id=str(item.get("id", "")),
        media_id=media_id,
        text=str(item.get("text") or ""),
        from_id=str(sender.get("id") or ""),
        username=str(item.get("username") or sender.get("username") or ""),
        created_at=parse_published_at(item.get("timestamp") or ""),
    )


async def _media_to_poll(db: AsyncIOMotorDatabase, channel: dict[str, Any], now: datetime) -> list[str]:
    """Reels whose comments could still earn a DM: ones with a rule on, plus recent ones under the default."""
    channel_id = str(channel["channel_id"])
    reel_rules = await list_reel_rules(db, channel_id)
    media = {mid for mid, stored in reel_rules.items() if stored.rule.enabled}
    default = channel.get(DEFAULT_FIELD) or {}
    if (default.get("rule") or {}).get("enabled"):
        cursor = db.videos.find(
            {"channel_id": channel_id, "instagram_media_id": {"$exists": True}},
            {"instagram_media_id": 1, "published_at": 1},
        ).sort("published_at", -1)
        for video in await cursor.limit(POLL_REEL_LIMIT).to_list(POLL_REEL_LIMIT):
            published = parse_published_at(video.get("published_at"))
            media_id = str(video.get("instagram_media_id") or "")
            if media_id and published is not None and now - published <= PRIVATE_REPLY_WINDOW:
                # A reel with its own rule switched off stays off.
                if media_id not in reel_rules or reel_rules[media_id].rule.enabled:
                    media.add(media_id)
    return sorted(media)


async def poll_channel(db: AsyncIOMotorDatabase, manager: Any, channel: dict[str, Any], now: datetime) -> int:
    """Queue DMs for comments the webhook hasn't delivered. Returns how many were queued."""
    channel_id = str(channel["channel_id"])
    resolved = await manager.get_service_and_user_id(channel_id, prefer=PROVIDER_FACEBOOK)
    if resolved is None or resolved[0].provider != PROVIDER_FACEBOOK:
        return 0
    reader = resolved[0]
    rng = random.Random()
    queued = 0
    for media_id in await _media_to_poll(db, channel, now):
        try:
            items = await asyncio.to_thread(reader.get_recent_comments, media_id)
        except requests.RequestException as exc:
            logger.warning("[CommentDM] Reading comments on %s failed: %s", media_id, graph_error_message(exc))
            continue
        for item in items:
            outcome = await enqueue(db, channel, _event_from_graph(item, media_id), source="poll", now=now, rng=rng)
            queued += outcome == QUEUED
    return queued


async def _poll_all(db: AsyncIOMotorDatabase, manager: Any, now: datetime) -> None:
    for channel_id in await channels_with_rules(db):
        channel = await db.channels.find_one({"channel_id": channel_id})
        if channel:
            queued = await poll_channel(db, manager, channel, now)
            if queued:
                logger.info("[CommentDM] Poll queued %d DM(s) on %s", queued, channel_id)


async def _send_due(db: AsyncIOMotorDatabase, manager: Any) -> None:
    for _ in range(MAX_SENDS_PER_TICK):
        now = now_ist()
        event = await claim_due(db, now)
        if event is None:
            return
        await send_one(db, manager, event, now)


async def run_comment_dm_worker(db: AsyncIOMotorDatabase, manager: Any) -> None:
    """Long-running loop: sends due DMs every few seconds and polls for missed comments every few minutes."""
    now = now_ist()
    failed = await fail_interrupted(db, now - STUCK_AFTER, now)
    if failed:
        logger.warning("[CommentDM] %d DM(s) were cut off by a restart and marked failed", failed)
    next_poll = now
    logger.info("[CommentDM] Worker started (tick %ds, poll every %s)", TICK_SECONDS, POLL_INTERVAL)
    while True:
        try:
            await _send_due(db, manager)
            if now_ist() >= next_poll:
                await _poll_all(db, manager, now_ist())
                next_poll = now_ist() + POLL_INTERVAL
        except asyncio.CancelledError:
            logger.info("[CommentDM] Worker shutting down")
            break
        except Exception as exc:
            logger.exception("[CommentDM] Worker pass failed")
            await get_error_service(db).log_error(
                feature="Comment DM Worker", message="Comment-to-DM pass failed", exception=exc
            )
        await asyncio.sleep(TICK_SECONDS)
