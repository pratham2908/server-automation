"""YouTube per-video analysis and audience, as stored by the YouTube sync.

Answers in the same shapes as the Instagram endpoints so the dashboard reuses
its charts: ``media_id`` is the YouTube video id, ``skip_rate`` is the share of
views that swiped away, and a timeline day's ``follows`` is subscribers gained —
measured by YouTube per video, not estimated.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import verify_api_key
from app.models.content_analysis import (
    ContentAnalysisOut,
    ReelAnalysisOut,
    ReelDayOut,
    ReelHourlyOut,
    ReelHourOut,
    ReelTimelineOut,
    group_out,
    number_or_none,
    rounded,
    slot_out,
)
from app.services.instagram_content_analysis import (
    ReelSummary,
    content_groups,
    flatten_params,
    parse_published_at,
    per_1k,
    posting_slots,
)
from app.services.instagram_reel_hourly import HOURLY_METRICS, MAX_GAP_HOURS, MAX_HOURS, MetricReading, hourly_gains
from app.services.video_properties import parse_property_filter, property_filter_query
from app.timezone import assume_utc, now_ist, to_ist_iso

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/youtube",
    tags=["youtube-insights"],
    dependencies=[Depends(verify_api_key)],
)

# How much of the channel's own daily history the audience view returns.
CHANNEL_DAYS_SHOWN = 90


def _watch_through(meta: dict[str, object]) -> float | None:
    percent = number_or_none(meta.get("avg_percentage_viewed"))
    return None if percent is None else round(percent / 100, 3)


@router.get("/content-analysis", response_model=ContentAnalysisOut)
async def get_content_analysis(
    channel_id: str,
    properties: str | None = Query(None, description="Only videos with these tags: p1:v1,v2;p2:v3"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> ContentAnalysisOut:
    """Per-video speed, hook and hold, plus posting-time and topic/format breakdowns."""
    videos: list[ReelAnalysisOut] = []
    summaries: list[ReelSummary] = []
    params: dict[str, dict[str, list[str]]] = {}
    cursor = db.videos.find(
        {
            "channel_id": channel_id,
            "youtube_video_id": {"$ne": None},
            "status": "published",
            # Every card below is then built from just the tagged videos, posting times and groups included.
            **property_filter_query(parse_property_filter(properties)),
        },
        {
            "youtube_video_id": 1,
            "video_id": 1,
            "title": 1,
            "thumbnail_url": 1,
            "category": 1,
            "content_params": 1,
            "properties": 1,
            "published_at": 1,
            "metadata": 1,
        },
    )
    async for doc in cursor:
        meta = doc.get("metadata") or {}
        video_id = doc["youtube_video_id"]
        published = parse_published_at(doc.get("published_at"))
        views = number_or_none(meta.get("views"))
        # Shares and subscribers come from Analytics, days behind the live view
        # count; rated against Analytics' own views they compare like with like.
        analytics_views = number_or_none(meta.get("analytics_views")) or views
        through = _watch_through(meta)
        video = ReelAnalysisOut(
            media_id=video_id,
            video_id=doc.get("video_id"),
            title=doc.get("title"),
            thumbnail_url=doc.get("thumbnail_url"),
            category=doc.get("category"),
            published_at=to_ist_iso(published),
            views=views,
            skip_rate=number_or_none(meta.get("swiped_away_rate")),
            avg_watch_seconds=number_or_none(meta.get("avg_view_duration_seconds")),
            duration_seconds=number_or_none(meta.get("duration_seconds")),
            watch_through=through,
            shares_per_1k=rounded(per_1k(number_or_none(meta.get("shares")), analytics_views)),
            likes_per_1k=rounded(per_1k(number_or_none(meta.get("likes")), views)),
            comments_per_1k=rounded(per_1k(number_or_none(meta.get("comments")), views)),
            likes=number_or_none(meta.get("likes")),
            comments=number_or_none(meta.get("comments")),
            shares=number_or_none(meta.get("shares")),
            watch_hours=number_or_none(meta.get("estimated_hours_watched")),
            subscribers_gained=number_or_none(meta.get("subscribers_gained")),
            engaged_views=number_or_none(meta.get("engaged_views")),
            views_24h=number_or_none(meta.get("views_24h")),
            views_48h=number_or_none(meta.get("views_48h")),
            views_7d=number_or_none(meta.get("views_7d")),
            hours_to_half_views=number_or_none(meta.get("hours_to_half_views")),
            hours_to_90_views=number_or_none(meta.get("hours_to_90_views")),
            picking_up=meta.get("picking_up") if isinstance(meta.get("picking_up"), bool) else None,
            properties=doc.get("properties") or {},
        )
        videos.append(video)
        summaries.append(
            ReelSummary(
                media_id=video_id,
                category=video.category,
                published_at=published,
                views=video.views,
                skip_rate=video.skip_rate,
                watch_through=through,
                shares_per_1k=video.shares_per_1k,
                saves_per_1k=None,
                views_24h=video.views_24h,
            )
        )
        params[video_id] = flatten_params(doc.get("content_params"))

    hours, weekdays = posting_slots(summaries, now_ist())
    videos.sort(key=lambda v: v.published_at or "", reverse=True)
    return ContentAnalysisOut(
        channel_id=channel_id,
        reels=videos,
        posting_hours=[slot_out(s) for s in hours],
        posting_weekdays=[slot_out(s) for s in weekdays],
        groups=[group_out(g) for g in content_groups(summaries, params)],
    )


@router.get("/videos/{video_id}/timeline", response_model=ReelTimelineOut)
async def get_video_timeline(
    channel_id: str, video_id: str, db: AsyncIOMotorDatabase = Depends(get_db)
) -> ReelTimelineOut:
    """A video's views and subscribers gained per day, as YouTube Analytics reports them (two to three days behind)."""
    cursor = db.youtube_video_daily.find({"channel_id": channel_id, "youtube_video_id": video_id}).sort("date", 1)
    days = [
        ReelDayOut(
            date=doc["date"],
            views=doc.get("views") or 0.0,
            follows=doc.get("subscribers_gained"),
            rough=False,
            status="measured",
            subscribers_lost=doc.get("subscribers_lost"),
            minutes_watched=doc.get("minutes_watched"),
            swiped_away_rate=doc.get("swiped_away_rate"),
        )
        async for doc in cursor
    ]
    return ReelTimelineOut(media_id=video_id, days=days)


@router.get("/videos/{video_id}/hourly", response_model=ReelHourlyOut)
async def get_video_hourly(channel_id: str, video_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> ReelHourlyOut:
    """A video's views, likes and comments gained in each IST hour, from the sync's live-count readings."""
    video = await db.videos.find_one({"channel_id": channel_id, "youtube_video_id": video_id}, {"published_at": 1})
    published = parse_published_at(video.get("published_at")) if video else None
    since = now_ist() - timedelta(hours=MAX_HOURS) - timedelta(days=1)
    cursor = db.youtube_video_snapshots.find(
        {"channel_id": channel_id, "youtube_video_id": video_id, "at": {"$gte": since}}
    ).sort("at", 1)
    readings = [
        MetricReading(
            at=assume_utc(snap["at"]),
            values={m: float(snap[m]) for m in HOURLY_METRICS if isinstance(snap.get(m), int | float)},
        )
        async for snap in cursor
    ]
    hours = [ReelHourOut(start=h.start.isoformat(), **h.values) for h in hourly_gains(published, readings)]
    return ReelHourlyOut(media_id=video_id, max_gap_hours=MAX_GAP_HOURS, hours=hours)


class RetentionPointOut(BaseModel):
    # Position in the video, 0 = start, 1 = end.
    at: float
    # Share of viewers still watching there; above 1 where people rewatch.
    watching: float
    # Against similar YouTube videos: 0.5 is typical, higher is better.
    relative: float | None = None


class RetentionOut(BaseModel):
    media_id: str
    fetched_at: str | None = None
    points: list[RetentionPointOut] = []


@router.get("/videos/{video_id}/retention", response_model=RetentionOut)
async def get_video_retention(
    channel_id: str, video_id: str, db: AsyncIOMotorDatabase = Depends(get_db)
) -> RetentionOut:
    """The video's audience-retention curve; empty until YouTube has enough viewers to report one."""
    doc = await db.youtube_retention.find_one({"channel_id": channel_id, "youtube_video_id": video_id})
    if not doc:
        return RetentionOut(media_id=video_id)
    return RetentionOut(
        media_id=video_id,
        fetched_at=to_ist_iso(doc.get("fetched_at")),
        points=[RetentionPointOut(**p) for p in doc.get("points") or []],
    )


class ShareRowOut(BaseModel):
    value: str
    count: float


class AgeGenderOut(BaseModel):
    age: str
    gender: str
    percent: float


class ChannelDayOut(BaseModel):
    date: str
    views: float | None = None
    engaged_views: float | None = None
    minutes_watched: float | None = None
    subscribers_gained: float | None = None
    subscribers_lost: float | None = None


class YouTubeAudienceOut(BaseModel):
    channel_id: str
    fetched_at: str | None = None
    period_days: int | None = None
    # Each list is absent (None) when YouTube didn't answer that breakdown, and
    # empty when it answered with nothing (it withholds small audiences).
    age_gender: list[AgeGenderOut] | None = None
    countries: list[ShareRowOut] | None = None
    traffic_sources: list[ShareRowOut] | None = None
    devices: list[ShareRowOut] | None = None
    subscribed: list[ShareRowOut] | None = None
    days: list[ChannelDayOut] = []


@router.get("/audience", response_model=YouTubeAudienceOut)
async def get_audience(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> YouTubeAudienceOut:
    """The last four weeks' audience breakdowns and the channel's daily views and subscribers."""
    doc = await db.youtube_audience.find_one({"channel_id": channel_id}) or {}
    since = (now_ist() - timedelta(days=CHANNEL_DAYS_SHOWN)).date().isoformat()
    days = [
        ChannelDayOut(**{k: v for k, v in d.items() if k in ChannelDayOut.model_fields})
        async for d in db.youtube_channel_daily.find({"channel_id": channel_id, "date": {"$gte": since}}).sort(
            "date", 1
        )
    ]
    fetched = doc.get("fetched_at")
    return YouTubeAudienceOut(
        channel_id=channel_id,
        fetched_at=to_ist_iso(fetched) if isinstance(fetched, datetime) else None,
        period_days=doc.get("period_days"),
        age_gender=doc.get("age_gender"),
        countries=doc.get("countries"),
        traffic_sources=doc.get("traffic_sources"),
        devices=doc.get("devices"),
        subscribed=doc.get("subscribed"),
        days=days,
    )
