"""Views over time for a channel or one of its videos — the data behind the Studio-style views card.

Hourly ranges (48h, 7d) are built from our sync readings and can chart any
counted metric; daily ranges (28d, 90d) chart views from the platform's own
per-day numbers. See ``app/services/view_series.py``.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.database import get_channel_platform, get_db
from app.dependencies import verify_api_key
from app.services.instagram_content_analysis import parse_published_at
from app.services.instagram_reel_hourly import MAX_HOURS, MetricReading
from app.services.instagram_reel_snapshot import MEASURED_ONLY
from app.services.view_series import RANGES, SeriesPoint, daily_points, hourly_points, series_total
from app.timezone import IST, assume_utc, now_ist

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}",
    tags=["views-series"],
    dependencies=[Depends(verify_api_key)],
)

Metric = Literal["views", "likes", "comments", "shares", "saves"]
# Hourly ranges reach back at most MAX_HOURS; a day more finds the reading before the first hour.
_READ_BACK = timedelta(hours=MAX_HOURS + 24)


class SeriesPointOut(BaseModel):
    start: str
    value: float | None = None
    rough: bool = False


class ViewsSeriesOut(BaseModel):
    channel_id: str
    video_id: str | None = None
    range: str
    granularity: Literal["hour", "day"]
    metric: str
    # "readings": our hourly sync readings. "platform": the platform's own per-day numbers.
    source: Literal["readings", "platform"]
    points: list[SeriesPointOut]
    # Sum over the buckets that have a value, and how many do.
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


async def _hourly_readings(
    db: AsyncIOMotorDatabase, channel_id: str, platform: str, video_id: str | None, metric: str
) -> tuple[list[MetricReading], Any]:
    since = now_ist() - _READ_BACK
    if video_id is None:
        cursor = db.channel_view_snapshots.find({"channel_id": channel_id, "at": {"$gte": since}}).sort("at", 1)
        published = None
    elif platform == "instagram":
        cursor = db.instagram_reel_snapshots.find(
            {"channel_id": channel_id, "media_id": video_id, "at": {"$gte": since}, **MEASURED_ONLY}
        ).sort("at", 1)
        video = await db.videos.find_one(
            {"channel_id": channel_id, "instagram_media_id": video_id}, {"published_at": 1}
        )
        published = parse_published_at(video.get("published_at")) if video else None
    else:
        cursor = db.youtube_video_snapshots.find(
            {"channel_id": channel_id, "youtube_video_id": video_id, "at": {"$gte": since}}
        ).sort("at", 1)
        video = await db.videos.find_one({"channel_id": channel_id, "youtube_video_id": video_id}, {"published_at": 1})
        published = parse_published_at(video.get("published_at")) if video else None
    readings = [
        MetricReading(at=assume_utc(doc["at"]), values={metric: float(doc[metric])})
        async for doc in cursor
        if isinstance(doc.get(metric), int | float)
    ]
    return readings, published


async def _daily_views(
    db: AsyncIOMotorDatabase, channel_id: str, platform: str, video_id: str | None
) -> tuple[dict[str, float | None], set[str], str | None]:
    """Views per date from the platform's own daily numbers, the dates spread across a long gap, and a note."""
    values: dict[str, float | None] = {}
    rough: set[str] = set()
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


async def _views_series(
    db: AsyncIOMotorDatabase, channel_id: str, video_id: str | None, range_key: str, metric: str
) -> ViewsSeriesOut:
    if range_key not in RANGES:
        raise HTTPException(status_code=422, detail=f"range must be one of {', '.join(RANGES)}")
    granularity, size = RANGES[range_key]
    channel = await _channel(db, channel_id)
    platform = get_channel_platform(channel)
    now = now_ist()

    if granularity == "hour":
        readings, published = await _hourly_readings(db, channel_id, platform, video_id, metric)
        points = hourly_points(published, readings, metric, now, size)
        last = readings[-1].at.astimezone(IST).isoformat() if readings else None
        source: Literal["readings", "platform"] = "readings"
        note = None
    else:
        if metric != "views":
            raise HTTPException(status_code=422, detail="Daily ranges show views only")
        values, rough, note = await _daily_views(db, channel_id, platform, video_id)
        today: date = now.astimezone(IST).date()
        points = daily_points(values, rough, today, size)
        reported = [d for d, v in values.items() if v is not None]
        last = max(reported) if reported else None
        source = "platform" if not (platform == "instagram" and video_id) else "readings"

    total, known = series_total(points)
    return ViewsSeriesOut(
        channel_id=channel_id,
        video_id=video_id,
        range=range_key,
        granularity=granularity,
        metric=metric,
        source=source,
        points=[_out(p) for p in points],
        total=round(total, 1),
        known_points=known,
        last_updated=last,
        note=note,
    )


@router.get("/views", response_model=ViewsSeriesOut)
async def get_channel_views(
    channel_id: str,
    range: str = Query("48h", description="48h | 7d (hourly) or 28d | 90d (daily)"),
    metric: Metric = Query("views"),
    video_id: str | None = Query(None, description="A video's platform id; omit for the whole channel"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ViewsSeriesOut:
    """Views (or likes, comments, shares, saves on hourly ranges) per hour or per day."""
    return await _views_series(db, channel_id, video_id, range, metric)
