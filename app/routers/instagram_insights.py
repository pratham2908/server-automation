"""Instagram audience demographics and stories, as stored by the Instagram sync."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import verify_api_key
from app.services.instagram_audience import story_is_live
from app.timezone import now_ist, to_ist_iso

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
