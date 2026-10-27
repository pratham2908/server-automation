"""Views over time for a channel or one of its videos — the data behind the Studio-style views card.

Hourly ranges (24h, 48h, 7d) are built from our sync readings, in hour or
quarter-hour bars, and can chart any counted metric; daily ranges (28d, 90d) chart views from the platform's own
per-day numbers. See ``app/services/view_series.py``.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.database import get_channel_platform, get_db
from app.dependencies import verify_api_key
from app.services.instagram_content_analysis import parse_published_at
from app.services.instagram_reel_hourly import HOUR, MAX_HOURS, MetricReading
from app.services.instagram_reel_snapshot import MEASURED_ONLY
from app.services.view_series import (
    DAY,
    LEVEL_SNAPSHOT_FIELDS,
    RANGES,
    STEPS,
    Align,
    Curve,
    Measure,
    SeriesPoint,
    Step,
    curve_points,
    daily_from_readings,
    daily_points,
    hourly_points,
    last_known,
    series_total,
    window_starts,
)
from app.services.views_top_videos import MEDIA_FIELDS, ViewsTopVideosOut, get_channel_top_videos
from app.services.views_trending import TrendingVideosOut, get_trending_videos
from app.timezone import IST, assume_utc, now_ist

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}",
    tags=["views-series"],
    dependencies=[Depends(verify_api_key)],
)

Metric = Literal["views", "likes", "comments", "shares", "saves", "skip_rate", "avg_watch_seconds", "watch_through"]
# Hourly ranges reach back at most MAX_HOURS; a day more finds the reading before the first hour.
_READ_BACK = timedelta(hours=MAX_HOURS + 24)


class SeriesPointOut(BaseModel):
    start: str
    value: float | None = None
    rough: bool = False
    offset_hours: float | None = None


class ViewsSeriesOut(BaseModel):
    channel_id: str
    video_id: str | None = None
    range: str
    granularity: Literal["hour", "day"]
    # The bar width of an hourly range; daily ranges ignore it.
    step: Step = "1h"
    align: Align = "clock"
    measure: Measure = "gain"
    metric: str
    # "readings": our hourly sync readings. "platform": the platform's own per-day numbers.
    source: Literal["readings", "platform"]
    points: list[SeriesPointOut]
    # Sum over the buckets that have a value (the newest value, for a running total), and how many have one.
    total: float
    known_points: int
    # The newest reading or reported day behind the series, for "updated …".
    last_updated: str | None = None
    # The platform's daily numbers arrive late; what that means for the newest days.
    note: str | None = None


def _out(point: SeriesPoint) -> SeriesPointOut:
    return SeriesPointOut(**asdict(point))


async def _channel(db: AsyncIOMotorDatabase, channel_id: str) -> dict[str, Any]:
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    return channel


async def _published_at(
    db: AsyncIOMotorDatabase, channel_id: str, platform: str, video_id: str | None
) -> datetime | None:
    """When the video went up; None for the whole channel or a video we never learned the time of."""
    if video_id is None:
        return None
    field = MEDIA_FIELDS.get(platform, "youtube_video_id")
    video = await db.videos.find_one({"channel_id": channel_id, field: video_id}, {"published_at": 1})
    return parse_published_at(video.get("published_at")) if video else None


async def _duration_seconds(db: AsyncIOMotorDatabase, channel_id: str, media_id: str | None) -> float | None:
    video = await db.videos.find_one(
        {"channel_id": channel_id, "instagram_media_id": media_id}, {"metadata.duration_seconds": 1}
    )
    value = ((video or {}).get("metadata") or {}).get("duration_seconds")
    return float(value) if isinstance(value, int | float) else None


async def _hourly_readings(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    platform: str,
    video_id: str | None,
    metric: str,
    since: datetime | None = None,
) -> tuple[list[MetricReading], datetime | None]:
    since = since or now_ist() - _READ_BACK
    field = LEVEL_SNAPSHOT_FIELDS.get(metric, metric)
    scale = 1.0
    if metric == "watch_through":
        # Without the reel's length there is no share to draw.
        length = await _duration_seconds(db, channel_id, video_id)
        if not length:
            return [], await _published_at(db, channel_id, platform, video_id)
        scale = 1 / length
    if video_id is None:
        cursor = db.channel_view_snapshots.find({"channel_id": channel_id, "at": {"$gte": since}})
    elif platform == "threads":
        cursor = db.threads_post_snapshots.find({"channel_id": channel_id, "media_id": video_id, "at": {"$gte": since}})
    elif platform == "instagram":
        cursor = db.instagram_reel_snapshots.find(
            {"channel_id": channel_id, "media_id": video_id, "at": {"$gte": since}, **MEASURED_ONLY}
        )
    else:
        cursor = db.youtube_video_snapshots.find(
            {"channel_id": channel_id, "youtube_video_id": video_id, "at": {"$gte": since}}
        )
    readings = [
        MetricReading(at=assume_utc(doc["at"]), values={metric: float(doc[field]) * scale})
        async for doc in cursor.sort("at", 1)
        if isinstance(doc.get(field), int | float)
    ]
    return readings, await _published_at(db, channel_id, platform, video_id)


async def _daily_views(
    db: AsyncIOMotorDatabase, channel_id: str, platform: str, video_id: str | None
) -> tuple[dict[str, float | None], set[str], str | None]:
    """Views per date from the platform's own daily numbers, the dates spread across a long gap, and a note."""
    values: dict[str, float | None] = {}
    rough: set[str] = set()
    if platform == "threads" and video_id is None:
        async for d in db.threads_account_daily.find({"channel_id": channel_id}, {"date": 1, "views": 1}):
            values[d["date"]] = d.get("views")
        return values, rough, "Threads' own daily totals for the account. Today fills in as the day goes."
    if platform == "threads":
        video = await db.videos.find_one({"channel_id": channel_id, "threads_media_id": video_id}, {"published_at": 1})
        cursor = db.threads_post_snapshots.find({"channel_id": channel_id, "media_id": video_id}).sort("at", 1)
        readings = [
            MetricReading(at=assume_utc(doc["at"]), values={"views": float(doc["views"])})
            async for doc in cursor
            if isinstance(doc.get("views"), int | float)
        ]
        published = parse_published_at(video.get("published_at")) if video else None
        values.update(daily_from_readings(published, readings, "views"))
        return values, rough, "Threads reports a post's lifetime total only, so its days are built from our readings."
    if platform == "instagram" and video_id is None:
        async for d in db.instagram_account_daily.find({"channel_id": channel_id}, {"date": 1, "total_views": 1}):
            values[d["date"]] = d.get("total_views")
        return (
            values,
            rough,
            "Instagram's own daily totals (reels, posts and stories). Today fills in once the day closes.",
        )
    if platform == "instagram":
        async for d in db.instagram_reel_daily.find({"channel_id": channel_id, "media_id": video_id}):
            values[d["date"]] = d.get("views")
            if d.get("rough"):
                rough.add(d["date"])
        return values, rough, "Per day from our readings; a rough day was spread across a long gap between them."
    if video_id is None:
        async for d in db.youtube_channel_daily.find({"channel_id": channel_id}, {"date": 1, "views": 1}):
            values[d["date"]] = d.get("views")
    else:
        async for d in db.youtube_video_daily.find({"channel_id": channel_id, "youtube_video_id": video_id}):
            values[d["date"]] = d.get("views")
    return values, rough, "YouTube Analytics' daily numbers, which arrive two to three days late."


async def _curve_series(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    platform: str,
    video_id: str | None,
    metric: str,
    granularity: str,
    size: int,
    step: Step,
    align: Align,
    measure: Measure,
) -> tuple[list[SeriesPoint], str | None]:
    """A since-posted or running-total series, read off the readings' cumulative curve at bucket edges."""
    now = now_ist()
    bucket = STEPS[step] if granularity == "hour" else DAY
    count = int(size * HOUR / bucket) if granularity == "hour" else size
    # The publish time anchors a since-posted window and is the curve's free zero.
    published = await _published_at(db, channel_id, platform, video_id)
    if align == "posted" and published is None:
        raise HTTPException(status_code=422, detail="Since posted needs the video's publish time, which isn't known")
    starts = window_starts(align, bucket, count, now, published)
    # A day before the window, to find the reading before its first edge.
    since = (starts[0] if starts else now) - DAY
    readings, _ = await _hourly_readings(db, channel_id, platform, video_id, metric, since=since)
    points = curve_points(
        Curve(published, readings, metric), starts, bucket, measure, now, published if align == "posted" else None
    )
    last = readings[-1].at.astimezone(IST).isoformat() if readings else None
    return points, last


async def _views_series(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    video_id: str | None,
    range_key: str,
    metric: str,
    step: Step = "1h",
    align: Align = "clock",
    measure: Measure = "gain",
) -> ViewsSeriesOut:
    if range_key not in RANGES:
        raise HTTPException(status_code=422, detail=f"range must be one of {', '.join(RANGES)}")
    if align == "posted" and video_id is None:
        raise HTTPException(status_code=422, detail="Since posted applies to one video, not the whole channel")
    granularity, size = RANGES[range_key]
    channel = await _channel(db, channel_id)
    platform = get_channel_platform(channel)
    now = now_ist()

    is_level = metric in LEVEL_SNAPSHOT_FIELDS
    if is_level:
        if video_id is None or platform != "instagram":
            raise HTTPException(status_code=422, detail="Skip rate and watch time are recorded per Instagram reel")
        # A level has no gain: the chart reads where it stood at the end of each bucket.
        measure = "total"

    if align == "posted" or measure == "total":
        points, last = await _curve_series(
            db, channel_id, platform, video_id, metric, granularity, size, step, align, measure
        )
        source: Literal["readings", "platform"] = "readings"
        note = (
            "Read off our own readings at each bucket's edge, so the platform's late daily numbers aren't used."
            if granularity == "day"
            else None
        )
    elif granularity == "hour":
        readings, published = await _hourly_readings(db, channel_id, platform, video_id, metric)
        points = hourly_points(published, readings, metric, now, size, STEPS[step])
        last = readings[-1].at.astimezone(IST).isoformat() if readings else None
        source = "readings"
        note = None
    else:
        if metric != "views":
            raise HTTPException(status_code=422, detail="Daily ranges show views only")
        values, rough, note = await _daily_views(db, channel_id, platform, video_id)
        today: date = now.astimezone(IST).date()
        points = daily_points(values, rough, today, size)
        reported = [d for d, v in values.items() if v is not None]
        last = max(reported) if reported else None
        source = "readings" if platform in ("instagram", "threads") and video_id else "platform"

    total, known = series_total(points)
    if measure == "total":
        total = last_known(points) or 0.0
    return ViewsSeriesOut(
        channel_id=channel_id,
        video_id=video_id,
        range=range_key,
        granularity=granularity,
        step=step,
        align=align,
        measure=measure,
        metric=metric,
        source=source,
        points=[_out(p) for p in points],
        total=round(total, 1),
        known_points=known,
        last_updated=last,
        note=note,
    )


@router.get("/views/top-videos", response_model=ViewsTopVideosOut)
async def get_views_top_videos(
    channel_id: str,
    range: str = Query("48h", description="24h | 48h | 7d (hourly) or 28d | 90d (daily)"),
    bucket: str | None = Query(None, description="ISO hour/quarter-hour start or date for a single candle"),
    limit: int = Query(6, ge=1, le=20),
    step: Step = Query("1h", description="Bar width of an hourly range: 1h | 15m"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ViewsTopVideosOut:
    """Top videos that gained the most views in the selected duration or specific candle."""
    return await get_channel_top_videos(db, channel_id, range, bucket, limit, step)


@router.get("/views/trending", response_model=TrendingVideosOut)
async def get_views_trending(
    channel_id: str,
    hours: int = Query(24, ge=1, le=168, description="How far back the gain is counted"),
    limit: int = Query(5, ge=1, le=20),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> TrendingVideosOut:
    """Videos that gained the most views in the last *hours*, old ones included."""
    return await get_trending_videos(db, await _channel(db, channel_id), hours, limit)


@router.get("/views", response_model=ViewsSeriesOut)
async def get_channel_views(
    channel_id: str,
    range: str = Query("48h", description="24h | 48h | 7d (hourly) or 28d | 90d (daily)"),
    metric: Metric = Query("views"),
    video_id: str | None = Query(None, description="A video's platform id; omit for the whole channel"),
    step: Step = Query("1h", description="Bar width of an hourly range: 1h | 15m"),
    align: Align = Query("clock", description="clock: IST clock buckets; posted: buckets since the video went up"),
    measure: Measure = Query("gain", description="gain: added in each bucket; total: cumulative at its end"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ViewsSeriesOut:
    """Views (or likes, comments, shares, saves) per hour, quarter-hour or day — gained, or as a running total."""
    return await _views_series(db, channel_id, video_id, range, metric, step, align, measure)
