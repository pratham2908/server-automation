"""Sync a Threads channel: its posts, their numbers, and the account's own totals.

Threads has no field expansion for insights on the post listing, so each post's
numbers are one call. Every post is read every sync — that is what lets the
channel's hourly total be a true sum — but a reading is *stored* only for posts
in their first week, or once a day after, so stored history grows with new posts
rather than with the back catalogue (the same rule Instagram reels follow).

The account side is what Threads reports itself: views per day, the follower
count, and (from 100 followers) who the followers are.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import requests

from app.logger import get_logger
from app.services.auto_sync_schedule import needs_reel_insights
from app.services.channel_view_snapshots import record_channel_totals
from app.services.instagram_content_analysis import parse_published_at
from app.services.threads import ThreadsService
from app.services.threads_insights_parse import (
    account_daily_views,
    account_totals,
    demographics,
    post_insights,
    window_since,
)
from app.timezone import IST, assume_utc

logger = get_logger(__name__)

# Threads' own names for a post's counts, mapped to the names our charts use.
_SNAPSHOT_FIELDS = {
    "views": "views",
    "likes": "likes",
    "replies": "comments",
    "reposts": "reposts",
    "quotes": "quotes",
    "shares": "shares",
}
_DEMOGRAPHIC_BREAKDOWNS = ("country", "city", "age", "gender")
_ACCOUNT_DAYS = 30
_TITLE_CHARS = 90


def snapshot_counts(insights: Mapping[str, float]) -> dict[str, int]:
    """One post's counts under our chart names (``replies`` reads as ``comments``)."""
    return {ours: int(insights[theirs]) for theirs, ours in _SNAPSHOT_FIELDS.items() if theirs in insights}


def channel_totals(post_ids: Sequence[str], insights: Mapping[str, Mapping[str, float]]) -> dict[str, int] | None:
    """The profile's summed counts at one sync, or None if any post came back without views.

    A total missing a post would read as a drop in the chart, so it isn't recorded at all.
    """
    totals = {name: 0 for name in _SNAPSHOT_FIELDS.values()}
    for pid in post_ids:
        row = insights.get(pid)
        if row is None or "views" not in row:
            return None
        for name, value in snapshot_counts(row).items():
            totals[name] += value
    totals["posts"] = len(post_ids)
    return totals


def _title(text: str) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= _TITLE_CHARS else one_line[: _TITLE_CHARS - 1].rstrip() + "…"


async def _read_insights(service: ThreadsService, post_ids: Sequence[str]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for pid in post_ids:
        try:
            out[pid] = post_insights(await asyncio.to_thread(service.post_insights, pid))
        except requests.HTTPError as exc:
            # One post refusing (a repost facade, a post mid-processing) must not sink the sync;
            # it simply has no reading this time, which also keeps the channel total from being written.
            logger.warning("Threads insights for post %s failed: %s", pid, exc)
    return out


async def _sync_account(db: Any, service: ThreadsService, channel_id: str, now: datetime) -> dict[str, Any]:
    """Views per day, the follower count, and demographics once a day."""
    today = now.astimezone(IST).date()
    since = window_since(today, _ACCOUNT_DAYS)
    out: dict[str, Any] = {}
    try:
        body = await asyncio.to_thread(
            service.account_insights,
            "views",
            int(datetime(since.year, since.month, since.day, tzinfo=IST).timestamp()),
            int(now.timestamp()),
        )
        for day, views in account_daily_views(body).items():
            await db.threads_account_daily.update_one(
                {"channel_id": channel_id, "date": day},
                {"$set": {"views": int(views), "updated_at": now}},
                upsert=True,
            )
        totals = account_totals(await asyncio.to_thread(service.account_insights, "followers_count"))
        if "followers_count" in totals:
            out["followers_count"] = int(totals["followers_count"])
            await db.threads_account_daily.update_one(
                {"channel_id": channel_id, "date": today.isoformat()},
                {"$set": {"followers_count": out["followers_count"], "updated_at": now}},
                upsert=True,
            )
    except requests.HTTPError as exc:
        logger.warning("Threads account insights failed for '%s': %s", channel_id, exc)

    audience = await db.threads_audience.find_one({"channel_id": channel_id}, {"updated_at": 1})
    stale = not audience or now - assume_utc(audience["updated_at"]) >= timedelta(hours=23)
    if stale:
        breakdowns: dict[str, list[dict[str, Any]]] = {}
        for breakdown in _DEMOGRAPHIC_BREAKDOWNS:
            try:
                breakdowns[breakdown] = demographics(await asyncio.to_thread(service.follower_demographics, breakdown))
            except requests.HTTPError as exc:
                # Threads refuses demographics below 100 followers; that is a normal state, not a fault.
                logger.info("Threads %s demographics unavailable for '%s': %s", breakdown, channel_id, exc)
        await db.threads_audience.update_one(
            {"channel_id": channel_id},
            {"$set": {**breakdowns, "updated_at": now}},
            upsert=True,
        )
    return out


async def sync_threads_channel(
    db: Any, service: ThreadsService, channel_id: str, now: datetime, incremental: bool = False
) -> dict[str, Any]:
    """Bring one Threads channel's posts, readings and account numbers up to date."""
    posts = await asyncio.to_thread(service.list_posts)
    post_ids = [str(p["id"]) for p in posts]
    insights = await _read_insights(service, post_ids)

    await record_channel_totals(db, channel_id, "threads", channel_totals(post_ids, insights), now)

    known = {
        v["threads_media_id"]: v
        async for v in db.videos.find(
            {"channel_id": channel_id, "threads_media_id": {"$in": post_ids}},
            {"threads_media_id": 1, "insights_synced_at": 1, "published_at": 1},
        )
    }
    inserted = 0
    for post in posts:
        pid = str(post["id"])
        published = parse_published_at(post.get("timestamp"))
        counts = snapshot_counts(insights.get(pid, {}))
        video = known.get(pid)
        due = not incremental or needs_reel_insights(
            published,
            assume_utc(video["insights_synced_at"]) if video and video.get("insights_synced_at") else None,
            now,
        )
        if counts and due:
            await db.threads_post_snapshots.insert_one({"channel_id": channel_id, "media_id": pid, "at": now, **counts})

        fields: dict[str, Any] = {
            "threads_permalink": post.get("permalink"),
            "threads_media_type": post.get("media_type"),
            "updated_at": now,
            **{f"metadata.{k}": v for k, v in counts.items()},
        }
        if counts and due:
            fields["insights_synced_at"] = now
        if post.get("thumbnail_url"):
            fields["thumbnail_url"] = post["thumbnail_url"]
        if video:
            await db.videos.update_one({"channel_id": channel_id, "threads_media_id": pid}, {"$set": fields})
            continue
        # A post made outside the app (or before the channel was connected): recorded so its numbers are charted.
        text = str(post.get("text") or "")
        await db.videos.insert_one(
            {
                "channel_id": channel_id,
                "video_id": str(uuid.uuid4()),
                "threads_media_id": pid,
                "title": _title(text) or "Threads post",
                "description": text,
                "tags": [post["topic_tag"]] if post.get("topic_tag") else [],
                "category": "Uncategorized",
                "status": "published",
                "published_at": published,
                "created_at": now,
                "threads_permalink": post.get("permalink"),
                "threads_media_type": post.get("media_type"),
                "thumbnail_url": post.get("thumbnail_url"),
                # Dotted keys are an update operator's syntax; an insert needs the nested document.
                "metadata": counts,
                "insights_synced_at": now if counts else None,
                "updated_at": now,
            }
        )
        inserted += 1

    account = await _sync_account(db, service, channel_id, now)
    return {
        "ok": True,
        "posts": len(posts),
        "synced": inserted,
        "read": len(insights),
        "followers_count": account.get("followers_count"),
    }
