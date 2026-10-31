"""Fallback videos per reel: list them, pick reels and fallbacks, set and remove a rule."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.database import get_channel_platform, get_db
from app.dependencies import verify_api_key
from app.services.fallback_video import PENDING, TRIGGERED, FallbackRule, ends_at, rule_fields
from app.services.trial_graduation import MAX_UNTIL_HOURS, TrialCheck
from app.timezone import now_ist, to_ist_iso

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/fallbacks",
    tags=["fallback-videos"],
    dependencies=[Depends(verify_api_key)],
)

_VIDEO_FIELDS = {
    "video_id": 1,
    "title": 1,
    "thumbnail_url": 1,
    "status": 1,
    "scheduled_at": 1,
    "published_at": 1,
    "created_at": 1,
    "fallback_for": 1,
}


class VideoBrief(BaseModel):
    video_id: str
    title: str | None = None
    thumbnail_url: str | None = None
    status: str | None = None
    scheduled_at: str | None = None
    published_at: str | None = None
    # Set on a ready video already reserved as another reel's fallback.
    fallback_for: str | None = None


class FallbackOut(BaseModel):
    reel: VideoBrief
    fallback: VideoBrief
    check: TrialCheck
    state: str
    # When the next hourly check runs; null until the reel publishes.
    due_at: str | None = None
    # When the last check runs; null until the reel publishes.
    ends_at: str | None = None
    checked_at: str | None = None
    metrics: dict[str, float] = {}
    result: dict[str, Any] | None = None
    error: str | None = None
    met_after_hours: float | None = None
    publish_at: str | None = None
    permalink: str | None = None


class FallbackListOut(BaseModel):
    fallbacks: list[FallbackOut]


class VideoListOut(BaseModel):
    videos: list[VideoBrief]


def _iso(value: Any) -> str | None:
    return to_ist_iso(value) if isinstance(value, datetime) else None


def _brief(doc: dict[str, Any] | None, video_id: str) -> VideoBrief:
    doc = doc or {}
    return VideoBrief(
        video_id=video_id,
        title=doc.get("title"),
        thumbnail_url=doc.get("thumbnail_url"),
        status=doc.get("status"),
        scheduled_at=_iso(doc.get("scheduled_at")),
        published_at=_iso(doc.get("published_at")),
        fallback_for=doc.get("fallback_for"),
    )


def _out(reel: dict[str, Any], fallback: dict[str, Any] | None) -> FallbackOut:
    rule = reel["fallback"]
    published = reel.get("published_at") if reel.get("status") == "published" else None
    return FallbackOut(
        reel=_brief(reel, reel["video_id"]),
        fallback=_brief(fallback, rule["video_id"]),
        check=TrialCheck.model_validate(rule["check"]),
        state=reel.get("fallback_state") or PENDING,
        due_at=_iso(reel.get("fallback_due_at")) if reel.get("fallback_state") == PENDING else None,
        ends_at=_iso(ends_at(published, rule["check"])) if isinstance(published, datetime) else None,
        checked_at=_iso(reel.get("fallback_checked_at")),
        metrics=reel.get("fallback_metrics") or {},
        result=reel.get("fallback_result"),
        error=reel.get("fallback_error"),
        met_after_hours=reel.get("fallback_met_after_hours"),
        publish_at=_iso(reel.get("fallback_publish_at")),
        permalink=reel.get("fallback_permalink"),
    )


async def _instagram_channel(db: AsyncIOMotorDatabase, channel_id: str) -> dict[str, Any]:
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(404, "Channel not found")
    if get_channel_platform(channel) != "instagram":
        raise HTTPException(422, "Fallback videos are for Instagram channels")
    return dict(channel)


@router.get("", response_model=FallbackListOut)
async def list_fallbacks(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> FallbackListOut:
    """Every reel with a fallback rule: waiting and being checked first, then finished ones, newest first."""
    reels = (
        await db.videos.find({"channel_id": channel_id, "fallback": {"$exists": True, "$ne": None}})
        .sort("updated_fallback_at", -1)
        .to_list(length=200)
    )
    ids = [r["fallback"]["video_id"] for r in reels]
    fallbacks = {
        d["video_id"]: d
        async for d in db.videos.find({"channel_id": channel_id, "video_id": {"$in": ids}}, _VIDEO_FIELDS)
    }
    rows = [_out(r, fallbacks.get(r["fallback"]["video_id"])) for r in reels]
    rows.sort(key=lambda r: r.state != PENDING)  # stable: keeps newest-first within each part
    return FallbackListOut(fallbacks=rows)


@router.get("/reels", response_model=VideoListOut)
async def reels_for_rules(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> VideoListOut:
    """Reels a rule can go on: scheduled ones, soonest first, then ones posted inside the longest window."""
    since = now_ist() - timedelta(hours=MAX_UNTIL_HOURS)
    scheduled = (
        await db.videos.find({"channel_id": channel_id, "status": {"$in": ["scheduled", "queued"]}}, _VIDEO_FIELDS)
        .sort("scheduled_at", 1)
        .to_list(length=100)
    )
    posted = (
        await db.videos.find(
            {"channel_id": channel_id, "status": "published", "published_at": {"$gte": since}}, _VIDEO_FIELDS
        )
        .sort("published_at", -1)
        .to_list(length=100)
    )
    return VideoListOut(videos=[_brief(v, v["video_id"]) for v in [*scheduled, *posted]])


@router.get("/candidates", response_model=VideoListOut)
async def fallback_candidates(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> VideoListOut:
    """Ready videos that could be a fallback, newest first; reserved ones say whose fallback they are."""
    docs = (
        await db.videos.find(
            {"channel_id": channel_id, "status": "ready", "r2_object_key": {"$nin": [None, ""]}}, _VIDEO_FIELDS
        )
        .sort("created_at", -1)
        .to_list(length=200)
    )
    return VideoListOut(videos=[_brief(v, v["video_id"]) for v in docs])


@router.put("/{video_id}", response_model=FallbackOut)
async def set_fallback(
    channel_id: str, video_id: str, body: FallbackRule, db: AsyncIOMotorDatabase = Depends(get_db)
) -> FallbackOut:
    """Put a fallback on a reel, or change the one it has while it is still waiting."""
    channel = await _instagram_channel(db, channel_id)
    reel = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if not reel:
        raise HTTPException(404, "Reel not found")
    fallback = await db.videos.find_one({"channel_id": channel_id, "video_id": body.fallback_video_id})
    now = now_ist()
    try:
        fields = rule_fields(reel, fallback, body, get_channel_platform(channel), now)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    assert fallback is not None  # rule_fields refuses a missing one
    previous = (reel.get("fallback") or {}).get("video_id")
    if previous and previous != body.fallback_video_id:
        await db.videos.update_one(
            {"channel_id": channel_id, "video_id": previous, "fallback_for": video_id}, {"$set": {"fallback_for": None}}
        )
    await db.videos.update_one({"_id": reel["_id"]}, {"$set": {**fields, "updated_fallback_at": now}})
    await db.videos.update_one({"_id": fallback["_id"]}, {"$set": {"fallback_for": video_id}})
    updated = await db.videos.find_one({"_id": reel["_id"]})
    fresh = await db.videos.find_one({"_id": fallback["_id"]}, _VIDEO_FIELDS)
    return _out(dict(updated or {}), fresh)


@router.delete("/{video_id}")
async def remove_fallback(channel_id: str, video_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    """Take the rule off a reel. A fallback already queued stays queued: unschedule it from the Scheduled tab."""
    reel = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if not reel or not reel.get("fallback"):
        raise HTTPException(404, "This reel has no fallback")
    fallback_id = reel["fallback"]["video_id"]
    if reel.get("fallback_state") != TRIGGERED:
        await db.videos.update_one(
            {"channel_id": channel_id, "video_id": fallback_id, "fallback_for": video_id},
            {"$set": {"fallback_for": None}},
        )
    unset = {
        k: ""
        for k in (
            "fallback",
            "fallback_state",
            "fallback_due_at",
            "fallback_attempts",
            "fallback_error",
            "fallback_checked_at",
            "fallback_metrics",
            "fallback_result",
            "fallback_permalink",
            "fallback_met_after_hours",
            "fallback_triggered_at",
            "fallback_publish_at",
            "fallback_emailed",
            "fallback_email_attempts",
            "updated_fallback_at",
        )
    }
    await db.videos.update_one({"_id": reel["_id"]}, {"$unset": unset})
    return {"deleted": True, "kept_queued": reel.get("fallback_state") == TRIGGERED}
