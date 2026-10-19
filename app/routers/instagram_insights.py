"""Instagram audience demographics, stories and per-reel analysis, as stored by the Instagram sync."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Literal

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
from app.services.instagram_audience import story_is_live
from app.services.instagram_content_analysis import (
    GroupStat,
    PostingSlot,
    ReelSummary,
    content_groups,
    day_status,
    flatten_params,
    parse_published_at,
    per_1k,
    posting_slots,
    watch_through,
)
from app.services.instagram_reel_hourly import HOURLY_METRICS, MAX_GAP_HOURS, MAX_HOURS, MetricReading, hourly_gains
from app.services.instagram_reel_snapshot import MEASURED_ONLY
from app.timezone import assume_utc, now_ist, to_ist_iso

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/instagram",
    tags=["instagram-insights"],
    dependencies=[Depends(verify_api_key)],
)


class DemographicRow(BaseModel):
    value: str
    count: int


class AudienceOut(BaseModel):
    """``audiences[reached|engaged|followers][this_week|this_month][age|gender|country|city]``."""

    channel_id: str
    fetched_at: str | None = None
    audiences: dict[str, dict[str, dict[str, list[DemographicRow]]]] = {}


class StoryOut(BaseModel):
    story_id: str
    timestamp: str | None = None
    media_type: str | None = None
    thumbnail_url: str | None = None
    permalink: str | None = None
    caption: str | None = None
    fetched_at: str | None = None
    live: bool
    metrics: dict[str, int] = {}
    navigation: dict[str, int] = {}
    # Why Instagram held back the numbers, e.g. "Not enough viewers" on a fresh story.
    insights_note: str | None = None


@router.get("/audience", response_model=AudienceOut)
async def get_audience(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> AudienceOut:
    """The latest demographics snapshot; empty until the first Instagram sync stores one."""
    doc = await db.instagram_audience.find_one({"channel_id": channel_id}, sort=[("fetched_at", -1)])
    if not doc:
        return AudienceOut(channel_id=channel_id)
    return AudienceOut(
        channel_id=channel_id, fetched_at=to_ist_iso(doc.get("fetched_at")), audiences=doc.get("audiences") or {}
    )


def _story_out(doc: dict[str, Any], now: datetime) -> StoryOut:
    posted = doc.get("posted_at")
    return StoryOut(
        story_id=doc["story_id"],
        timestamp=to_ist_iso(posted),
        media_type=doc.get("media_type"),
        thumbnail_url=doc.get("thumbnail_url"),
        permalink=doc.get("permalink"),
        caption=doc.get("caption"),
        fetched_at=to_ist_iso(doc.get("fetched_at")),
        live=story_is_live(posted, now),
        metrics=doc.get("metrics") or {},
        navigation=doc.get("navigation") or {},
        insights_note=doc.get("insights_note"),
    )


@router.get("/stories", response_model=list[StoryOut])
async def get_stories(
    channel_id: str,
    limit: int = Query(30, ge=1, le=200),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> list[StoryOut]:
    """Stories captured while they were up, newest first."""
    now = now_ist()
    cursor = db.instagram_stories.find({"channel_id": channel_id}).sort("posted_at", -1).limit(limit)
    return [_story_out(doc, now) async for doc in cursor]


@router.get("/reels/{media_id}/timeline", response_model=ReelTimelineOut)
async def get_reel_timeline(
    channel_id: str, media_id: str, db: AsyncIOMotorDatabase = Depends(get_db)
) -> ReelTimelineOut:
    """A reel's views and estimated follows, profile visits and bio-link taps, day by day (IST)."""
    today = now_ist().date()
    cursor = db.instagram_reel_daily.find({"channel_id": channel_id, "media_id": media_id}).sort("date", 1)
    days = [
        ReelDayOut(
            date=doc["date"],
            views=doc.get("views") or 0.0,
            follows=doc.get("follows"),
            profile_visits=doc.get("profile_visits"),
            bio_link_taps=doc.get("bio_link_taps"),
            rough=bool(doc.get("rough")),
            status=day_status(doc["date"], doc.get("follows") is not None, today),
        )
        async for doc in cursor
    ]
    return ReelTimelineOut(media_id=media_id, days=days)


@router.get("/reels/{media_id}/hourly", response_model=ReelHourlyOut)
async def get_reel_hourly(channel_id: str, media_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> ReelHourlyOut:
    """A reel's views, likes, comments, shares and saves gained in each IST hour, from the sync's readings."""
    video = await db.videos.find_one({"channel_id": channel_id, "instagram_media_id": media_id}, {"published_at": 1})
    published = parse_published_at(video.get("published_at")) if video else None
    # One reading before the window is needed to place the window's first hour.
    since = now_ist() - timedelta(hours=MAX_HOURS) - timedelta(days=1)
    cursor = db.instagram_reel_snapshots.find(
        {"channel_id": channel_id, "media_id": media_id, "at": {"$gte": since}, **MEASURED_ONLY}
    ).sort("at", 1)
    readings = [
        MetricReading(
            at=assume_utc(snap["at"]),
            values={m: float(snap[m]) for m in HOURLY_METRICS if isinstance(snap.get(m), int | float)},
        )
        async for snap in cursor
    ]
    hours = [ReelHourOut(start=h.start.isoformat(), **h.values) for h in hourly_gains(published, readings)]
    return ReelHourlyOut(media_id=media_id, max_gap_hours=MAX_GAP_HOURS, hours=hours)


@router.get("/content-analysis", response_model=ContentAnalysisOut)
async def get_content_analysis(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> ContentAnalysisOut:
    """Per-reel speed, hook and follow rate, plus posting-time and topic/format breakdowns."""
    reels: list[ReelAnalysisOut] = []
    summaries: list[ReelSummary] = []
    params: dict[str, dict[str, list[str]]] = {}
    cursor = db.videos.find(
        {"channel_id": channel_id, "instagram_media_id": {"$ne": None}, "status": "published"},
        {
            "instagram_media_id": 1,
            "video_id": 1,
            "title": 1,
            "thumbnail_url": 1,
            "category": 1,
            "content_params": 1,
            "published_at": 1,
            "metadata": 1,
        },
    )
    async for doc in cursor:
        meta = doc.get("metadata") or {}
        media_id = doc["instagram_media_id"]
        published = parse_published_at(doc.get("published_at"))
        views = number_or_none(meta.get("views"))
        through = watch_through(
            number_or_none(meta.get("avg_view_duration_seconds")), number_or_none(meta.get("duration_seconds"))
        )
        reel = ReelAnalysisOut(
            media_id=media_id,
            video_id=doc.get("video_id"),
            title=doc.get("title"),
            thumbnail_url=doc.get("thumbnail_url"),
            category=doc.get("category"),
            published_at=to_ist_iso(published),
            views=views,
            skip_rate=number_or_none(meta.get("skip_rate")),
            avg_watch_seconds=number_or_none(meta.get("avg_view_duration_seconds")),
            duration_seconds=number_or_none(meta.get("duration_seconds")),
            watch_through=None if through is None else round(through, 3),
            shares_per_1k=rounded(per_1k(number_or_none(meta.get("shares")), views)),
            saves_per_1k=rounded(per_1k(number_or_none(meta.get("saves")), views)),
            likes_per_1k=rounded(per_1k(number_or_none(meta.get("likes")), views)),
            comments_per_1k=rounded(per_1k(number_or_none(meta.get("comments")), views)),
            reach=number_or_none(meta.get("reach")),
            likes=number_or_none(meta.get("likes")),
            comments=number_or_none(meta.get("comments")),
            shares=number_or_none(meta.get("shares")),
            saves=number_or_none(meta.get("saves")),
            watch_hours=number_or_none(meta.get("estimated_hours_watched")),
            views_per_reach=number_or_none(meta.get("views_per_reach")),
            engagement_rate=number_or_none(meta.get("engagement_rate")),
            estimated_follows=number_or_none(meta.get("estimated_follows")),
            follow_quality=meta.get("follow_estimate_quality"),
            views_24h=number_or_none(meta.get("views_24h")),
            views_48h=number_or_none(meta.get("views_48h")),
            views_7d=number_or_none(meta.get("views_7d")),
            hours_to_half_views=number_or_none(meta.get("hours_to_half_views")),
            hours_to_90_views=number_or_none(meta.get("hours_to_90_views")),
            picking_up=meta.get("picking_up") if isinstance(meta.get("picking_up"), bool) else None,
        )
        reels.append(reel)
        summaries.append(
            ReelSummary(
                media_id=media_id,
                category=reel.category,
                published_at=published,
                views=reel.views,
                skip_rate=reel.skip_rate,
                watch_through=through,
                shares_per_1k=reel.shares_per_1k,
                saves_per_1k=reel.saves_per_1k,
                views_24h=reel.views_24h,
            )
        )
        params[media_id] = flatten_params(doc.get("content_params"))

    hours, weekdays = posting_slots(summaries, now_ist())
    reels.sort(key=lambda r: r.published_at or "", reverse=True)
    return ContentAnalysisOut(
        channel_id=channel_id,
        reels=reels,
        posting_hours=[slot_out(s) for s in hours],
        posting_weekdays=[slot_out(s) for s in weekdays],
        groups=[group_out(g) for g in content_groups(summaries, params)],
    )
