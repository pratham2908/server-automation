"""Reads every new comment for its channel's collectors, and keeps the counted comments' likes fresh.

Comments come from ``comment_log``, which the webhook, the comment-to-DM poll and the comment sync all
write, so a comment the webhook missed is still read. One AI call per comment answers every collector
that applies to its reel. A comment that no collector applies to is marked read without a call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services import comment_collector_store as store
from app.services import comment_collectors as cc
from app.services.instagram_tokens import PROVIDER_FACEBOOK
from app.timezone import assume_utc, now_ist

logger = get_logger(__name__)

TICK_SECONDS = 30
# Comments read per channel per pass; a backlog drains over a few passes rather than in one burst.
PER_PASS = 40
LIKES_EVERY = timedelta(minutes=10)  # how often the like schedule is checked (each comment has its own gap)
MAX_ATTEMPTS = 3
# Comments read at once: a read-back of thousands drains in about an hour without crowding the AI service.
PARALLEL = 4
BACKLOG_TICK_SECONDS = 2


class AnswerReader(Protocol):
    async def collect_answers(self, prompt: str) -> dict[str, Any]: ...


async def _reel(
    db: AsyncIOMotorDatabase, channel_id: str, media_id: str, cache: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """The reel's property tags, title and caption, read once per pass."""
    if media_id not in cache:
        video = (
            await db.videos.find_one(
                {"channel_id": channel_id, "instagram_media_id": media_id},
                {"properties": 1, "title": 1, "description": 1},
            )
            or {}
        )
        cache[media_id] = {
            "properties": video.get("properties") or {},
            "title": video.get("title"),
            "caption": video.get("description"),
        }
    return cache[media_id]


def _due_for(comment: dict[str, Any], collectors: Sequence[cc.Collector], properties: Any) -> list[cc.Collector]:
    """The collectors this comment hasn't been read for yet, that read its reel and were on when it was written."""
    read = comment.get("collected") or {}
    written = comment.get("commented_at")
    out = []
    for c in collectors:
        if c.id in read or not c.enabled:
            continue
        if isinstance(written, datetime) and assume_utc(written) < assume_utc(c.active_from):
            continue
        if cc.applies(c, str(comment.get("media_id") or ""), properties):
            out.append(c)
    return out


def _kind_due(comment: dict[str, Any], kinds_since: datetime | None) -> bool:
    written = comment.get("commented_at")
    return (
        kinds_since is not None
        and "kind" not in comment
        and (not isinstance(written, datetime) or assume_utc(written) >= assume_utc(kinds_since))
    )


async def read_comment(
    db: AsyncIOMotorDatabase,
    reader: AnswerReader,
    comment: dict[str, Any],
    collectors: Sequence[cc.Collector],
    reel: dict[str, Any],
    now: datetime,
    kinds_since: datetime | None = None,
) -> int:
    """Answer one comment for every collector it is due, and sort it into a kind if that's on.

    Returns how many collector answers were kept.
    """
    asked = _due_for(comment, collectors, reel.get("properties"))
    ask_kind = _kind_due(comment, kinds_since)
    comment_id = str(comment["comment_id"])
    # Collectors that don't read this reel still mark it, so it isn't looked at again every pass.
    asked_ids = {c.id for c in asked}
    already = comment.get("collected") or {}
    skipped = [c.id for c in collectors if c.enabled and c.id not in asked_ids and c.id not in already]
    await store.mark_read(db, comment_id, skipped, now)
    if not asked and not ask_kind:
        return 0
    text = str(comment.get("text") or "").strip()
    if not text:
        await store.mark_read(db, comment_id, [c.id for c in asked], now)
        if ask_kind:
            await store.record_kind(db, comment_id, "other", now)
        return 0
    prompt = cc.build_prompt(text, asked, reel.get("title"), caption=reel.get("caption"), ask_kind=ask_kind)
    try:
        reply = await reader.collect_answers(prompt)
    except Exception as exc:  # noqa: BLE001 - a model failure is retried, then given up on, never read as "no answer"
        attempts = int(comment.get("collect_attempts", 0)) + 1
        fields: dict[str, Any] = {"collect_attempts": attempts}
        if attempts >= MAX_ATTEMPTS:
            fields["collect_failed"] = True
        await db.comment_log.update_one({"comment_id": comment_id}, {"$set": fields})
        logger.warning("[Collectors] Reading comment %s failed (attempt %d): %s", comment_id, attempts, exc)
        return 0
    if ask_kind:
        await store.record_kind(db, comment_id, cc.parse_kind(reply), now)
    if not asked:
        return 0
    return await store.record_answers(db, comment, cc.parse_answers(reply, asked), asked, now)


async def read_channel(db: AsyncIOMotorDatabase, reader: AnswerReader, channel_id: str, now: datetime) -> int:
    """One pass over the channel's unread comments. Returns how many collector answers were kept."""
    return (await read_channel_pass(db, reader, channel_id, now))[0]


async def read_channel_pass(
    db: AsyncIOMotorDatabase, reader: AnswerReader, channel_id: str, now: datetime
) -> tuple[int, int]:
    """(answers kept, comments read) for one pass; a full pass means there is a backlog to keep draining."""
    collectors = [c for c in await store.list_collectors(db, channel_id) if c.enabled]
    since = await store.kinds_since(db, channel_id)
    if not collectors and since is None:
        return 0, 0
    cache: dict[str, dict[str, Any]] = {}
    comments = await store.unread_comments(db, channel_id, collectors, PER_PASS, kinds_since=since)
    for comment in comments:
        await _reel(db, channel_id, str(comment.get("media_id") or ""), cache)
    gate = asyncio.Semaphore(PARALLEL)

    async def one(comment: dict[str, Any]) -> int:
        async with gate:
            reel = cache[str(comment.get("media_id") or "")]
            return await read_comment(db, reader, comment, collectors, reel, now, since)

    return sum(await asyncio.gather(*(one(c) for c in comments))), len(comments)


async def refresh_channel_likes(db: AsyncIOMotorDatabase, manager: Any, channel_id: str, now: datetime) -> int:
    resolved = await manager.get_service_and_user_id(channel_id, prefer=PROVIDER_FACEBOOK)
    if resolved is None:
        return 0
    return await store.refresh_likes(db, resolved[0], channel_id, now)


async def run_comment_collector_worker(db: AsyncIOMotorDatabase, manager: Any, reader: AnswerReader | None) -> None:
    """Long-running loop: reads new comments every few seconds, checks likes every few minutes."""
    if reader is None:
        logger.warning("[Collectors] No AI service; collectors will not read comments")
        return
    await store.ensure_indexes(db)
    next_likes = now_ist()
    logger.info("[Collectors] Worker started (tick %ds)", TICK_SECONDS)
    while True:
        backlog = False
        try:
            for channel_id in await store.channels_to_read(db):
                _, read = await read_channel_pass(db, reader, channel_id, now_ist())
                backlog = backlog or read >= PER_PASS
                if now_ist() >= next_likes:
                    try:
                        await refresh_channel_likes(db, manager, channel_id, now_ist())
                    except Exception:  # noqa: BLE001 - likes are a nicety; never let them stop the reading
                        logger.exception("[Collectors] Reading likes on %s failed", channel_id)
            if now_ist() >= next_likes:
                next_likes = now_ist() + LIKES_EVERY
        except asyncio.CancelledError:
            logger.info("[Collectors] Worker shutting down")
            break
        except Exception:
            logger.exception("[Collectors] Worker pass failed")
        # A read-back keeps going without the pause; a quiet channel is looked at every half minute.
        await asyncio.sleep(BACKLOG_TICK_SECONDS if backlog else TICK_SECONDS)
