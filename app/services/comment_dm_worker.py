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
from typing import Any, Protocol

import requests
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import ValidationError

from app.logger import get_logger
from app.services import city_tally, comment_log, dm_outbox, dm_replies, meet_city
from app.services.comment_dm import (
    HOURLY_CAP,
    MAX_ATTEMPTS,
    PRIVATE_REPLY_WINDOW,
    RETRY_BACKOFF,
    SKIP_ALREADY_MESSAGED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_SKIPPED,
    CommentEvent,
    is_rate_limited,
    retry_at,
)
from app.services.comment_dm_store import (
    CITY_CHECK_FIELD,
    CITY_DONE,
    CITY_NONE,
    CITY_PENDING,
    DEFAULT_FIELD,
    NO_ASK,
    NO_CITY,
    QUEUED,
    channels_with_rules,
    claim_due,
    enqueue,
    fail_interrupted,
    message_fields,
    own_rules_by_media,
    sent_since,
    update_event,
)
from app.services.dm_content import DmMessage, fallback_text, has_template, main_message
from app.services.dm_sender import context_for, r2_signer
from app.services.error_reporting import get_error_service
from app.services.instagram import graph_error_code, graph_error_message
from app.services.instagram_content_analysis import parse_published_at
from app.services.instagram_tokens import PROVIDER_FACEBOOK, PROVIDER_INSTAGRAM
from app.timezone import now_ist

logger = get_logger(__name__)


class GeminiLike(Protocol):
    """The one model call the worker makes: which place a city rule's comment asks for."""

    async def extract_place(self, comment_text: str) -> meet_city.PlaceRead | None: ...


TICK_SECONDS = 10
POLL_INTERVAL = timedelta(minutes=5)
MAX_SENDS_PER_TICK = 20
# Longer than any one send takes, so only a send cut off by a restart is ever this old.
STUCK_AFTER = timedelta(minutes=10)
CAP_PAUSE = timedelta(minutes=5)
# Reels this recent are polled under a channel default; older ones can't take a private reply anyway.
POLL_REEL_LIMIT = 30


def event_message(event: dict[str, Any]) -> DmMessage:
    """The DM a queued event sends: its text plus the buttons, cards and chips copied from its rule."""
    return DmMessage(
        text=str(event.get("dm_text") or ""),
        buttons=event.get("buttons") or [],
        cards=event.get("cards") or [],
        quick_replies=event.get("quick_replies") or [],
    )


def _format(message: DmMessage) -> str:
    if message.cards:
        return "cards"
    return "buttons" if message.buttons or message.quick_replies else "text"


async def _send_dm(
    db: AsyncIOMotorDatabase, service: Any, ig_user_id: str, channel: dict[str, Any], event: dict[str, Any]
) -> dict[str, Any]:
    """Send the DM as its rule composed it. Returns the fields to record; raises when it can't send.

    Meta doesn't document which templates a private reply may carry. If it refuses one (anything but a
    rate limit), the same DM goes again as text with the links written out, so a button or a card never
    costs the commenter their message. Reply buttons and chips can't survive as text and are dropped.
    """
    comment_id = str(event["comment_id"])
    message = event_message(event)
    ctx = context_for(channel, message, r2_signer())
    built = main_message(message, ctx)
    if built is None:
        raise ValueError("the DM is empty")
    # Before sending, so a tap a second later finds the reply its button names.
    await dm_replies.register_message(db, str(channel["channel_id"]), message, now_ist())
    try:
        message_id = await asyncio.to_thread(service.send_private_reply, ig_user_id, comment_id, built)
        return {"dm_message_id": message_id, "dm_format": _format(message)}
    except requests.RequestException as exc:
        if not has_template(message) or is_rate_limited(graph_error_code(exc)):
            raise
        button_error = graph_error_message(exc)
    text = fallback_text(message, ctx)
    message_id = await asyncio.to_thread(service.send_private_reply, ig_user_id, comment_id, {"text": text})
    return {"dm_message_id": message_id, "dm_format": "text_fallback", "button_error": button_error}


async def _messaged_on_reel(db: AsyncIOMotorDatabase, event: dict[str, Any]) -> bool:
    """Whether this person already got (or is about to get) a DM on this reel from another comment."""
    found = await db.comment_dm_events.find_one(
        {
            "channel_id": event["channel_id"],
            "media_id": event.get("media_id"),
            "person": event.get("person"),
            "comment_id": {"$ne": event["comment_id"]},
            "status": {"$in": [STATUS_SENDING, STATUS_SENT]},
        },
        {"_id": 1},
    )
    return found is not None


async def resolve_city(
    db: AsyncIOMotorDatabase, gemini: GeminiLike | None, event: dict[str, Any], now: datetime
) -> dict[str, Any] | None:
    """Read the place a city rule's comment asks for and build its DM. None when it gets no DM (recorded)."""
    comment_id = str(event["comment_id"])
    text = str(event.get("text") or "")
    attempts = int(event.get("city_attempts", 0)) + 1
    read: meet_city.PlaceRead | None = None
    if gemini is not None:
        try:
            read = await gemini.extract_place(text)
        except Exception as exc:  # noqa: BLE001 - any model failure is retried, then given up on, never sent blind
            logger.warning("[CommentDM] Reading a city from comment %s failed: %s", comment_id, exc)
            if attempts < MAX_ATTEMPTS:
                await update_event(
                    db,
                    comment_id,
                    {"status": STATUS_PENDING, "send_after": now + RETRY_BACKOFF, "city_attempts": attempts},
                    now,
                )
                return None
    place = read.model_dump() if read is not None else None
    answer = meet_city.answer_for_read(read) if read is not None else None
    if answer is None or answer.outcome == "ignored":
        # No place, or one only mentioned in passing ("getting to battersea is a nightmare tho").
        reason = NO_CITY if read is None or read.asks else NO_ASK
        fields = {"status": STATUS_SKIPPED, "skip_reason": reason, CITY_CHECK_FIELD: CITY_NONE, "city_place": place}
        await update_event(db, comment_id, fields, now)
        return None
    city_map = meet_city.CityMap.model_validate(event.get("city_map") or {})
    again = await _messaged_on_reel(db, event)
    await city_tally.record_comment(
        db,
        channel_id=str(event["channel_id"]),
        comment_id=comment_id,
        media_id=str(event.get("media_id") or ""),
        peer_id=str(event.get("from_id") or ""),
        username=str(event.get("username") or ""),
        text=text,
        answer=answer,
        dm_queued=not again,
        commented_at=event.get("commented_at"),
        now=now,
        place=read.place if read is not None else None,
    )
    city = {CITY_CHECK_FIELD: CITY_DONE, "city": answer.model_dump(), "city_place": place}
    if again:
        await update_event(
            db, comment_id, {"status": STATUS_SKIPPED, "skip_reason": SKIP_ALREADY_MESSAGED, **city}, now
        )
        return None
    content = message_fields(meet_city.comment_dm(answer, city_map))
    await update_event(db, comment_id, {**content, **city}, now)
    return {**event, **content, **city}


async def send_one(
    db: AsyncIOMotorDatabase, manager: Any, event: dict[str, Any], now: datetime, gemini: GeminiLike | None = None
) -> str:
    """DM the commenter, then reply to the comment. Returns the status the event ends in."""
    comment_id = str(event["comment_id"])
    channel_id = str(event["channel_id"])
    if await sent_since(db, channel_id, now - timedelta(hours=1)) >= HOURLY_CAP:
        await update_event(db, comment_id, {"status": STATUS_PENDING, "send_after": now + CAP_PAUSE}, now)
        return STATUS_PENDING
    if event.get(CITY_CHECK_FIELD) == CITY_PENDING:
        resolved_event = await resolve_city(db, gemini, event, now)
        if resolved_event is None:
            stored = await db.comment_dm_events.find_one({"comment_id": comment_id}, {"status": 1})
            return str((stored or {}).get("status") or STATUS_SKIPPED)
        event = resolved_event
    # Instagram Login can send a private reply with the comment permission alone; Facebook Login also
    # needs instagram_manage_messages, which the shared token lacks — so prefer Instagram Login.
    resolved = await manager.get_service_and_user_id(channel_id, prefer=PROVIDER_INSTAGRAM)
    if resolved is None:
        await update_event(db, comment_id, {"status": STATUS_FAILED, "error": "channel has no Instagram token"}, now)
        return STATUS_FAILED
    service, ig_user_id = resolved
    attempts = int(event.get("attempts", 0)) + 1
    channel = await db.channels.find_one({"channel_id": channel_id}) or {"channel_id": channel_id}
    try:
        sent = await _send_dm(db, service, ig_user_id, channel, event)
    except (ValueError, ValidationError) as exc:
        # Can't be built here (a profile button with no username, an upload with no storage): no retry helps.
        error = str(exc)[:500]
        logger.warning("[CommentDM] DM for comment %s on %s can't be built: %s", comment_id, channel_id, error)
        await update_event(db, comment_id, {"status": STATUS_FAILED, "attempts": attempts, "error": error}, now)
        return STATUS_FAILED
    except requests.RequestException as exc:
        error = graph_error_message(exc)
        again = retry_at(attempts, now) if is_rate_limited(graph_error_code(exc)) else None
        if again is not None:
            retry = {"status": STATUS_PENDING, "send_after": again, "attempts": attempts, "error": error}
            await update_event(db, comment_id, retry, now)
            return STATUS_PENDING
        logger.warning("[CommentDM] DM for comment %s on %s failed: %s", comment_id, channel_id, error)
        await update_event(db, comment_id, {"status": STATUS_FAILED, "attempts": attempts, "error": error}, now)
        return STATUS_FAILED

    fields: dict[str, Any] = {"status": STATUS_SENT, "attempts": attempts, "sent_at": now, "error": None, **sent}
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
    reel_rules = await own_rules_by_media(db, channel_id)
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
            event = _event_from_graph(item, media_id)
            await comment_log.record_safely(db, channel, event, source=comment_log.SOURCE_POLL, now=now)
            outcome = await enqueue(db, channel, event, source="poll", now=now, rng=rng)
            queued += outcome == QUEUED
    return queued


async def _poll_all(db: AsyncIOMotorDatabase, manager: Any, now: datetime) -> None:
    for channel_id in await channels_with_rules(db):
        channel = await db.channels.find_one({"channel_id": channel_id})
        if channel:
            queued = await poll_channel(db, manager, channel, now)
            if queued:
                logger.info("[CommentDM] Poll queued %d DM(s) on %s", queued, channel_id)


async def _send_due(db: AsyncIOMotorDatabase, manager: Any, gemini: GeminiLike | None) -> None:
    for _ in range(MAX_SENDS_PER_TICK):
        now = now_ist()
        event = await claim_due(db, now)
        if event is None:
            break
        await send_one(db, manager, event, now, gemini)
    # Replies owed for taps on our buttons and chips ride the same tick.
    await dm_outbox.send_due(db, manager, now_ist)


async def run_comment_dm_worker(db: AsyncIOMotorDatabase, manager: Any, gemini: GeminiLike | None = None) -> None:
    """Long-running loop: sends due DMs every few seconds and polls for missed comments every few minutes."""
    now = now_ist()
    failed = await fail_interrupted(db, now - STUCK_AFTER, now) + await dm_outbox.fail_interrupted(db, now)
    if failed:
        logger.warning("[CommentDM] %d DM(s) were cut off by a restart and marked failed", failed)
    next_poll = now
    logger.info("[CommentDM] Worker started (tick %ds, poll every %s)", TICK_SECONDS, POLL_INTERVAL)
    while True:
        try:
            await _send_due(db, manager, gemini)
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
