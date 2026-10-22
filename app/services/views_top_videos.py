"""Top performing videos by views gained over a duration (hourly or daily range, or single candle).

Computes each video's view gain across the selected window:
- Hourly ranges (48h, 7d) or single hour bucket: derived from snapshot readings
  via ``app.services.instagram_reel_hourly.hourly_gains``.
- Daily ranges (28d, 90d) or single day bucket: aggregated from daily timelines
  (``instagram_reel_daily`` / ``youtube_video_daily``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Literal

from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.database import get_channel_platform
from app.services.instagram_reel_hourly import MetricReading, hourly_gains
from app.services.instagram_reel_snapshot import MEASURED_ONLY
from app.services.view_series import RANGES
from app.timezone import IST, assume_utc, now_ist


class TopVideoItem(BaseModel):
    video_id: str
    platform_id: str
    title: str
    thumbnail_url: str | None = None
    views_gained: float
    percentage: float = 0.0
    lifetime_views: int | None = None
    published_at: str | None = None
    url: str | None = None


class ViewsTopVideosOut(BaseModel):
    channel_id: str
    range: str
    granularity: Literal["hour", "day"]
    bucket: str | None = None
    total_views: float
    videos: list[TopVideoItem]


def _parse_hour_bucket(b: str) -> datetime | None:
    try:
        clean = b.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST).replace(minute=0, second=0, microsecond=0)
    except Exception:
        return None


def _format_video_url(platform: str, platform_id: str) -> str:
    if platform == "youtube":
        return f"https://www.youtube.com/watch?v={platform_id}"
    return f"https://www.instagram.com/reel/{platform_id}/"


async def get_channel_top_videos(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    range_key: str,
    bucket: str | None = None,
    limit: int = 6,
) -> ViewsTopVideosOut:
    """Return top videos ranked by views gained over the given range or specific candle bucket."""
    if range_key not in RANGES:
        raise HTTPException(status_code=422, detail=f"range must be one of {', '.join(RANGES)}")

    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    platform = get_channel_platform(channel)
    granularity, size = RANGES[range_key]
    now = now_ist()

    if granularity == "hour":
        return await _get_hourly_top_videos(db, channel_id, platform, range_key, size, bucket, limit, now)
    return await _get_daily_top_videos(db, channel_id, platform, range_key, size, bucket, limit, now)


async def _get_hourly_top_videos(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    platform: str,
    range_key: str,
    size: int,
    bucket: str | None,
    limit: int,
    now: datetime,
) -> ViewsTopVideosOut:
    since = now - timedelta(hours=size + 24)
    if platform == "instagram":
        cursor = db.instagram_reel_snapshots.find(
            {"channel_id": channel_id, "at": {"$gte": since}, **MEASURED_ONLY},
            {"media_id": 1, "at": 1, "views": 1},
        ).sort("at", 1)
        media_field = "instagram_media_id"
        id_key = "media_id"
    else:
        cursor = db.youtube_video_snapshots.find(
            {"channel_id": channel_id, "at": {"$gte": since}},
            {"youtube_video_id": 1, "at": 1, "views": 1},
        ).sort("at", 1)
        media_field = "youtube_video_id"
        id_key = "youtube_video_id"

    by_media: dict[str, list[MetricReading]] = {}
    async for doc in cursor:
        mid = doc.get(id_key)
        if not mid or not isinstance(doc.get("views"), int | float):
            continue
        by_media.setdefault(str(mid), []).append(
            MetricReading(at=assume_utc(doc["at"]), values={"views": float(doc["views"])})
        )

    end_hour = now.astimezone(IST).replace(minute=0, second=0, microsecond=0)
    valid_hours = {end_hour - timedelta(hours=size - i) for i in range(size)}

    if bucket:
        parsed_dt = _parse_hour_bucket(bucket)
        target_hours = {parsed_dt} if parsed_dt else set()
    else:
        target_hours = valid_hours

    gains_by_media: dict[str, float] = {}
    total_views_all_videos = 0.0

    for mid, readings in by_media.items():
        hg = hourly_gains(None, readings)
        tot = 0.0
        for g in hg:
            if g.start in target_hours:
                val = g.values.get("views")
                if val and val > 0:
                    tot += val
        if tot > 0:
            rounded_tot = round(tot, 1)
            gains_by_media[mid] = rounded_tot
            total_views_all_videos += rounded_tot

    sorted_media = sorted(gains_by_media.items(), key=lambda x: x[1], reverse=True)[:limit]
    top_ids = [m[0] for m in sorted_media]

    items = await _build_top_video_items(
        db, channel_id, platform, media_field, sorted_media, top_ids, total_views_all_videos
    )

    return ViewsTopVideosOut(
        channel_id=channel_id,
        range=range_key,
        granularity="hour",
        bucket=bucket,
        total_views=round(total_views_all_videos, 1),
        videos=items,
    )


async def _get_daily_top_videos(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    platform: str,
    range_key: str,
    size: int,
    bucket: str | None,
    limit: int,
    now: datetime,
) -> ViewsTopVideosOut:
    today: date = now.astimezone(IST).date()
    valid_dates = {(today - timedelta(days=size - 1 - i)).isoformat() for i in range(size)}

    if bucket:
        target_dates = [bucket[:10]]
    else:
        target_dates = list(valid_dates)

    if platform == "instagram":
        coll = db.instagram_reel_daily
        group_key = "$media_id"
        media_field = "instagram_media_id"
    else:
        coll = db.youtube_video_daily
        group_key = "$youtube_video_id"
        media_field = "youtube_video_id"

    pipeline = [
        {"$match": {"channel_id": channel_id, "date": {"$in": target_dates}}},
        {"$group": {"_id": group_key, "views": {"$sum": "$views"}}},
        {"$match": {"views": {"$gt": 0}}},
        {"$sort": {"views": -1}},
    ]

    all_res = await coll.aggregate(pipeline).to_list(1000)
    total_views_all_videos = round(sum(r["views"] for r in all_res), 1)

    top_res = all_res[:limit]
    sorted_media = [(str(r["_id"]), round(r["views"], 1)) for r in top_res]
    top_ids = [m[0] for m in sorted_media]

    items = await _build_top_video_items(
        db, channel_id, platform, media_field, sorted_media, top_ids, total_views_all_videos
    )

    return ViewsTopVideosOut(
        channel_id=channel_id,
        range=range_key,
        granularity="day",
        bucket=bucket,
        total_views=total_views_all_videos,
        videos=items,
    )


async def _build_top_video_items(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    platform: str,
    media_field: str,
    sorted_media: list[tuple[str, float]],
    top_ids: list[str],
    total_views: float,
) -> list[TopVideoItem]:
    if not top_ids:
        return []

    video_docs = await db.videos.find(
        {"channel_id": channel_id, media_field: {"$in": top_ids}},
        {
            "video_id": 1,
            "title": 1,
            "thumbnail_url": 1,
            media_field: 1,
            "published_at": 1,
            "metadata.views": 1,
        },
    ).to_list(len(top_ids) + 5)

    videos_by_pid = {str(v.get(media_field)): v for v in video_docs if v.get(media_field)}

    items: list[TopVideoItem] = []
    for pid, views in sorted_media:
        v = videos_by_pid.get(pid)
        vid = v.get("video_id") if v else pid
        title = v.get("title") if v and v.get("title") else f"Video {pid}"
        thumb = v.get("thumbnail_url") if v else None
        lifetime = (v.get("metadata") or {}).get("views") if v else None

        pub = v.get("published_at") if v else None
        pub_str: str | None = None
        if isinstance(pub, datetime | date):
            pub_str = pub.isoformat()
        elif isinstance(pub, str):
            pub_str = pub

        pct = round((views / total_views) * 100, 1) if total_views > 0 else 0.0

        items.append(
            TopVideoItem(
                video_id=vid,
                platform_id=pid,
                title=title,
                thumbnail_url=thumb,
                views_gained=views,
                percentage=pct,
                lifetime_views=int(lifetime) if isinstance(lifetime, int | float) else None,
                published_at=pub_str,
                url=_format_video_url(platform, pid),
            )
        )
    return items
