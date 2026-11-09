"""Assemble the daily digest: how each active channel did on one IST day.

An *active* channel is one that published something in the 7 days up to the end of
that day, the same test that puts a channel on 15-minute syncs. The numbers come from
what the syncs already store, so building a digest costs no platform calls:

* followers: the daily growth snapshot, this day against the one before;
* views: our channel readings (summed views across all posts) at the day's two edges;
* top posts: every post's own view readings, the same way;
* posts: what was published inside the day.

A figure we cannot work out (no reading near an edge, no snapshot for a day) is ``None``
and the email shows a dash, never a zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_channel_platform
from app.services.auto_sync_schedule import FRESH_REEL_DAYS
from app.services.views_top_videos import MEDIA_FIELDS, view_readings_by_media
from app.timezone import IST, assume_utc

# A reading further than this from a day's edge says nothing about that edge.
EDGE_TOLERANCE = timedelta(hours=6)
TOP_POSTS = 3


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Midnight to midnight of an IST date."""
    start = datetime.combine(day, time.min, tzinfo=IST)
    return start, start + timedelta(days=1)


def value_near(readings: Sequence[tuple[datetime, float]], moment: datetime) -> float | None:
    """The reading closest to *moment*, if one lies within EDGE_TOLERANCE."""
    best: tuple[timedelta, float] | None = None
    for at, value in readings:
        gap = abs(at - moment)
        if gap <= EDGE_TOLERANCE and (best is None or gap < best[0]):
            best = (gap, value)
    return best[1] if best else None


def gain_over_day(
    readings: Sequence[tuple[datetime, float]], start: datetime, end: datetime, *, born_in_day: bool = False
) -> float | None:
    """What a cumulative count gained between two edges.

    A post published inside the day has no reading at the start edge: it began at zero.
    """
    at_end = value_near(readings, end)
    if at_end is None:
        return None
    at_start = 0.0 if born_in_day else value_near(readings, start)
    if at_start is None:
        return None
    return max(0.0, at_end - at_start)


def video_url(platform: str, video: dict[str, Any]) -> str | None:
    media_id = video.get(MEDIA_FIELDS.get(platform, "youtube_video_id"))
    if platform == "youtube" and media_id:
        return f"https://www.youtube.com/watch?v={media_id}"
    if platform == "threads":
        return video.get("threads_permalink")
    if platform == "instagram" and media_id:
        return video.get("instagram_permalink") or f"https://www.instagram.com/reel/{media_id}/"
    return None


async def active_channel_ids(db: AsyncIOMotorDatabase, end: datetime) -> list[str]:
    since = end - timedelta(days=FRESH_REEL_DAYS)
    ids = await db.videos.distinct("channel_id", {"status": "published", "published_at": {"$gte": since, "$lt": end}})
    return sorted(ids)


async def _follower_change(db: AsyncIOMotorDatabase, channel_id: str, day: date) -> tuple[int | None, int | None]:
    """(followers at the end of the day, change over the day)."""
    keys = [(day - timedelta(days=1)).isoformat(), day.isoformat()]
    rows = {
        r["snapshot_date"]: r.get("subscribers")
        async for r in db.growth_snapshots.find({"channel_id": channel_id, "snapshot_date": {"$in": keys}})
    }
    now_count, before = rows.get(keys[1]), rows.get(keys[0])
    change = now_count - before if isinstance(now_count, int) and isinstance(before, int) else None
    return (now_count if isinstance(now_count, int) else None), change


async def _channel_views(db: AsyncIOMotorDatabase, channel_id: str, start: datetime, end: datetime) -> float | None:
    cursor = db.channel_view_snapshots.find(
        {"channel_id": channel_id, "at": {"$gte": start - EDGE_TOLERANCE, "$lte": end + EDGE_TOLERANCE}}
    )
    readings = [
        (assume_utc(d["at"]), float(d["views"])) async for d in cursor if isinstance(d.get("views"), int | float)
    ]
    return gain_over_day(readings, start, end)


async def _published(db: AsyncIOMotorDatabase, channel_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    cursor = db.videos.find(
        {"channel_id": channel_id, "status": "published", "published_at": {"$gte": start, "$lt": end}}
    )
    return [v async for v in cursor.sort("published_at", 1)]


async def _top_posts(
    db: AsyncIOMotorDatabase, channel_id: str, platform: str, start: datetime, end: datetime, born: set[str]
) -> list[dict[str, Any]]:
    by_media = await view_readings_by_media(db, channel_id, platform, start - EDGE_TOLERANCE)
    gains = {
        media_id: gain
        for media_id, readings in by_media.items()
        if (gain := gain_over_day(readings, start, end, born_in_day=media_id in born)) is not None and gain > 0
    }
    ranked = sorted(gains.items(), key=lambda kv: -kv[1])[:TOP_POSTS]
    if not ranked:
        return []
    field = MEDIA_FIELDS.get(platform, "youtube_video_id")
    docs = {
        str(v.get(field)): v
        async for v in db.videos.find({"channel_id": channel_id, field: {"$in": [m for m, _ in ranked]}})
    }
    return [
        {
            "title": (docs.get(media_id) or {}).get("title") or "Untitled post",
            "views": int(gain),
            "url": video_url(platform, docs.get(media_id) or {field: media_id}),
        }
        for media_id, gain in ranked
    ]


async def build_digest(db: AsyncIOMotorDatabase, day: date) -> dict[str, Any]:
    start, end = day_bounds(day)
    channels: list[dict[str, Any]] = []
    for channel_id in await active_channel_ids(db, end):
        channel = await db.channels.find_one({"channel_id": channel_id})
        if not channel:
            continue
        platform = get_channel_platform(channel)
        published = await _published(db, channel_id, start, end)
        field = MEDIA_FIELDS.get(platform, "youtube_video_id")
        followers, change = await _follower_change(db, channel_id, day)
        channels.append(
            {
                "channel_id": channel_id,
                "name": channel.get("name") or channel_id,
                "handle": channel.get("handle") or "",
                "platform": platform,
                "thumbnail_url": channel.get("thumbnail_url"),
                "followers": followers,
                "followers_change": change,
                "views": await _channel_views(db, channel_id, start, end),
                "posts": [
                    {"title": v.get("title") or "Untitled post", "url": video_url(platform, v)} for v in published
                ],
                "top_posts": await _top_posts(
                    db, channel_id, platform, start, end, {str(v[field]) for v in published if v.get(field)}
                ),
            }
        )
    channels.sort(key=lambda c: -(c["views"] or 0))
    return {"date": day.isoformat(), "channels": channels, "totals": totals_of(channels)}


def totals_of(channels: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Sums across channels; a figure is None only when no channel could supply it."""

    def total(key: str) -> float | None:
        values = [c[key] for c in channels if c.get(key) is not None]
        return sum(values) if values else None

    return {
        "channels": len(channels),
        "followers_change": total("followers_change"),
        "views": total("views"),
        "posts": sum(len(c["posts"]) for c in channels),
    }
