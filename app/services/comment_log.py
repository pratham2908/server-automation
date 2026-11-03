"""Every comment an Instagram channel receives, one row each: the source for commenter stats.

Rows arrive three ways, all keyed by comment id so a comment counts once however it came in:

- the webhook, the moment the comment is written;
- the comment-to-DM poll, for comments the webhook missed;
- a sync that reads comments back from Instagram. Its first run is the backfill; after that it runs every
  few hours, and only re-reads a post whose ``comments_count`` moved since it last looked, so it also
  covers channels that have no webhook.

The channel's own comments are kept, flagged ``own``, so the log shows the whole conversation; the stats
leave them out.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import requests
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.logger import get_logger
from app.services.comment_ai_reply import SKIP_NESTED, SKIP_OWN
from app.services.comment_dm import CommentEvent
from app.services.comment_dm_store import own_ids
from app.services.comment_reply_review import is_own_comment, own_identities
from app.services.instagram_content_analysis import parse_published_at

logger = get_logger(__name__)

# Rows live in ``comment_log``; ``comment_log_media`` keeps one row per post the sync has read, with the
# comments_count it saw then, so an unchanged post isn't read again.
SYNCED_FIELD = "comment_log_synced_at"

SOURCE_WEBHOOK = "webhook"
SOURCE_POLL = "poll"
SOURCE_SYNC = "sync"

# A post's whole comment section in one sync; Instagram pages it 50 threads at a time.
MAX_THREADS_PER_POST = 5000


def author_key(username: str, from_id: str = "") -> str:
    """Who wrote a comment, stable across sources: the handle, lowercased (the sync never sees ids)."""
    name = username.strip().lstrip("@").lower()
    return name or (f"id:{from_id}" if from_id else "")


def is_own(channel: dict[str, Any], username: str, from_id: str = "") -> bool:
    """The channel speaking, by account id where the source gives one, else by any handle it goes by."""
    return bool(from_id and from_id in own_ids(channel)) or is_own_comment(username, own_identities(channel))


def log_row(
    channel: dict[str, Any],
    *,
    comment_id: str,
    media_id: str,
    username: str,
    text: str,
    commented_at: datetime | None,
    source: str,
    now: datetime,
    from_id: str = "",
    parent_id: str | None = None,
    is_reply: bool | None = None,
) -> dict[str, Any] | None:
    """The row for one comment, or None when there's nothing to count (no id, or no author at all)."""
    author = author_key(username, from_id)
    if not comment_id or not media_id or not author:
        return None
    return {
        "comment_id": comment_id,
        "channel_id": str(channel["channel_id"]),
        "media_id": media_id,
        "author": author,
        "username": username.lstrip("@"),
        "from_id": from_id,
        "parent_id": parent_id,
        "is_reply": bool(parent_id) if is_reply is None else is_reply,
        "own": is_own(channel, username, from_id),
        "text": text,
        # A comment always has a time; when a source leaves it out, arrival is the closest stand-in.
        "commented_at": commented_at or now,
        "source": source,
        "created_at": now,
    }


async def _insert(db: AsyncIOMotorDatabase, row: dict[str, Any]) -> bool:
    if await db.comment_log.find_one({"comment_id": row["comment_id"]}, {"_id": 1}):
        return False
    try:
        await db.comment_log.insert_one(row)
    except DuplicateKeyError:
        # Webhook and poll delivered it at the same moment; the unique index let one through.
        return False
    return True


async def record(
    db: AsyncIOMotorDatabase, channel: dict[str, Any], event: CommentEvent, *, source: str, now: datetime
) -> bool:
    """Log a comment as it arrives. Returns whether it was new."""
    row = log_row(
        channel,
        comment_id=event.comment_id,
        media_id=event.media_id,
        username=event.username,
        from_id=event.from_id,
        parent_id=event.parent_id,
        text=event.text,
        commented_at=event.created_at,
        source=source,
        now=now,
    )
    return await _insert(db, row) if row else False


async def record_safely(
    db: AsyncIOMotorDatabase, channel: dict[str, Any], event: CommentEvent, *, source: str, now: datetime
) -> None:
    """``record`` for the DM and AI-reply paths: the stats log failing must never cost a reply."""
    try:
        await record(db, channel, event, source=source, now=now)
    except Exception as exc:  # noqa: BLE001 - logging only; the comment is still answered
        logger.warning("[CommentLog] Logging comment %s failed: %s", event.comment_id, exc)


def rows_from_threads(
    channel: dict[str, Any], media_id: str, threads: list[dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    """Log rows for one post's comment section as ``InstagramService.get_media_threads`` returns it."""
    rows: list[dict[str, Any]] = []

    def add(message: dict[str, Any], parent_id: str | None) -> None:
        row = log_row(
            channel,
            comment_id=str(message.get("comment_id") or ""),
            media_id=media_id,
            username=str(message.get("author") or ""),
            from_id=str(message.get("author_id") or ""),
            text=str(message.get("text") or ""),
            commented_at=parse_published_at(message.get("published_at")),
            parent_id=parent_id,
            source=SOURCE_SYNC,
            now=now,
        )
        if row:
            rows.append(row)

    for thread in threads:
        top = thread.get("top") or {}
        add(top, None)
        for reply in thread.get("replies") or []:
            add(reply, str(top.get("comment_id") or "") or None)
    return rows


async def sync_channel(
    db: AsyncIOMotorDatabase, channel: dict[str, Any], reader: Any, ig_user_id: str, now: datetime
) -> dict[str, int]:
    """Read back every post whose comment count moved since the last sync and log what's missing.

    The first run reads every post with comments, which is the backfill. A post that fails to read is
    left for the next run; it doesn't stop the others.
    """
    channel_id = str(channel["channel_id"])
    posts = await asyncio.to_thread(reader.list_comment_counts, ig_user_id)
    seen = {
        d["media_id"]: int(d.get("comments_count") or 0)
        async for d in db.comment_log_media.find({"channel_id": channel_id}, {"media_id": 1, "comments_count": 1})
    }
    stats = {"posts": len(posts), "read": 0, "added": 0, "failed": 0}
    for post in posts:
        media_id = str(post.get("id") or "")
        count = int(post.get("comments_count") or 0)
        if not media_id or count == 0 or seen.get(media_id) == count:
            continue
        try:
            threads = await asyncio.to_thread(reader.get_media_threads, media_id, MAX_THREADS_PER_POST)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("[CommentLog] Reading comments on %s (%s) failed: %s", media_id, channel_id, exc)
            stats["failed"] += 1
            continue
        stats["read"] += 1
        for row in rows_from_threads(channel, media_id, threads, now):
            stats["added"] += await _insert(db, row)
        await db.comment_log_media.update_one(
            {"channel_id": channel_id, "media_id": media_id},
            {"$set": {"comments_count": count, "synced_at": now}},
            upsert=True,
        )
    await db.channels.update_one({"channel_id": channel_id}, {"$set": {SYNCED_FIELD: now}})
    return stats


async def import_stored_comments(db: AsyncIOMotorDatabase, now: datetime) -> int:
    """Log comments the webhook delivered before this log existed, from the DM and AI-reply queues.

    Instagram can't read back a post that left the profile, so these are the only record of comments on
    archived reels. Runs after the sync: a comment both have keeps the sync's row, which knows its parent.
    """
    channels = {c["channel_id"]: c async for c in db.channels.find({"platform": "instagram"})}
    added = 0
    async for doc in db.comment_dm_events.find({}):
        channel = channels.get(doc.get("channel_id"))
        row = channel and log_row(
            channel,
            comment_id=str(doc.get("comment_id") or ""),
            media_id=str(doc.get("media_id") or ""),
            username=str(doc.get("username") or ""),
            from_id=str(doc.get("from_id") or ""),
            parent_id=doc.get("parent_id") or None,
            text=str(doc.get("text") or ""),
            commented_at=doc.get("commented_at"),
            source=SOURCE_WEBHOOK,
            now=now,
        )
        if row:
            added += await _insert(db, row)
    async for doc in db.comment_ai_reply_queue.find({}):
        channel = channels.get(doc.get("channel_id"))
        reason = doc.get("skip_reason")
        row = channel and log_row(
            channel,
            comment_id=str(doc.get("comment_id") or ""),
            media_id=str(doc.get("media_id") or ""),
            username=str(doc.get("username") or ""),
            text=str(doc.get("text") or ""),
            commented_at=doc.get("commented_at"),
            # The queue keeps no parent id, but it did note when a comment was a reply in a thread.
            is_reply=reason == SKIP_NESTED,
            source=SOURCE_WEBHOOK,
            now=now,
        )
        if row:
            # It also knew the channel's own comments by account id, which the row can't see any more.
            row["own"] = row["own"] or reason == SKIP_OWN
            added += await _insert(db, row)
    return added


# The sync's cadence: the webhook carries the live numbers, the sync only fills gaps.
SYNC_EVERY_SECONDS = 6 * 3600
# Let startup settle before the first (backfill) pass.
FIRST_SYNC_DELAY_SECONDS = 120


async def sync_instagram_channels(db: AsyncIOMotorDatabase, manager: Any, now: datetime) -> dict[str, dict[str, int]]:
    """Sync every Instagram channel that has a token; one channel failing doesn't stop the rest."""
    results: dict[str, dict[str, int]] = {}
    async for channel in db.channels.find({"platform": "instagram"}):
        channel_id = str(channel["channel_id"])
        try:
            resolved = await manager.get_service_and_user_id(channel_id)
            if resolved is None:
                continue
            results[channel_id] = await sync_channel(db, channel, resolved[0], resolved[1], now)
        except Exception as exc:  # noqa: BLE001 - a bad token on one channel mustn't stop the others
            logger.warning("[CommentLog] Sync of %s failed: %s", channel_id, exc)
    return results


async def run_comment_log_sync(db: AsyncIOMotorDatabase, manager: Any) -> None:
    """Loop forever: the first pass backfills, later ones catch comments the webhook missed."""
    from app.timezone import now_ist

    await asyncio.sleep(FIRST_SYNC_DELAY_SECONDS)
    while True:
        results = await sync_instagram_channels(db, manager, now_ist())
        added = {cid: r["added"] for cid, r in results.items() if r["added"] or r["failed"]}
        logger.info("[CommentLog] Sync done for %d channels; new comments: %s", len(results), added or "none")
        try:
            # Cheap and idempotent; after the first pass it finds nothing new.
            stored = await import_stored_comments(db, now_ist())
            if stored:
                logger.info("[CommentLog] Imported %d comments from the DM and AI-reply queues", stored)
        except Exception as exc:  # noqa: BLE001 - the loop must survive one bad pass
            logger.warning("[CommentLog] Importing stored comments failed: %s", exc)
        await asyncio.sleep(SYNC_EVERY_SECONDS)
