"""The owner's own properties on a channel's videos: define them per channel, tag each video.

See ``app/services/video_properties.py`` for the model. Definitions live on the channel
document (``video_properties``); a video's tags live on the video (``properties``).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key
from app.services.video_properties import (
    MAX_PROPERTIES,
    PropertyDef,
    PropertyDefIn,
    clean_assignment,
    define_properties,
    removed_ids,
)
from app.timezone import now_ist

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}",
    tags=["video-properties"],
    dependencies=[Depends(verify_api_key)],
)


class PropertiesOut(BaseModel):
    channel_id: str
    properties: list[PropertyDef]


class PropertiesIn(BaseModel):
    properties: list[PropertyDefIn] = Field(max_length=MAX_PROPERTIES)


class AssignmentIn(BaseModel):
    properties: dict[str, list[str]]


class AssignmentOut(BaseModel):
    video_id: str
    properties: dict[str, list[str]]


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


async def _defs(db: AsyncIOMotorDatabase, channel_id: str) -> list[PropertyDef]:
    channel = await db.channels.find_one({"channel_id": channel_id}, {"video_properties": 1})
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    return [PropertyDef.model_validate(p) for p in channel.get("video_properties") or []]


@router.get("/video-properties", response_model=PropertiesOut)
async def get_video_properties(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> PropertiesOut:
    """The channel's properties and their values."""
    return PropertiesOut(channel_id=channel_id, properties=await _defs(db, channel_id))


@router.put("/video-properties", response_model=PropertiesOut)
async def put_video_properties(
    channel_id: str, body: PropertiesIn, db: AsyncIOMotorDatabase = Depends(get_db)
) -> PropertiesOut:
    """Replace the channel's property list. Videos lose tags whose property or value was removed."""
    old = await _defs(db, channel_id)
    try:
        new = define_properties(body.properties, _new_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await db.channels.update_one(
        {"channel_id": channel_id}, {"$set": {"video_properties": [p.model_dump() for p in new]}}
    )

    # Untag the videos now, so filters and counts never see a value that no longer exists.
    gone_props, gone_values = removed_ids(old, new)
    unset: dict[str, Any] = {f"properties.{pid}": "" for pid in gone_props}
    if unset:
        await db.videos.update_many({"channel_id": channel_id}, {"$unset": unset})
    for pid, value_ids in gone_values.items():
        await db.videos.update_many(
            {"channel_id": channel_id, f"properties.{pid}": {"$in": list(value_ids)}},
            {"$pull": {f"properties.{pid}": {"$in": list(value_ids)}}},
        )
    return PropertiesOut(channel_id=channel_id, properties=new)


@router.put("/videos/{video_id}/properties", response_model=AssignmentOut)
async def put_video_assignment(
    channel_id: str, video_id: str, body: AssignmentIn, db: AsyncIOMotorDatabase = Depends(get_db)
) -> AssignmentOut:
    """Set one video's properties; unknown properties or values are dropped, not stored."""
    cleaned = clean_assignment(await _defs(db, channel_id), body.properties)
    result = await db.videos.update_one(
        {"channel_id": channel_id, "video_id": video_id},
        {"$set": {"properties": cleaned, "updated_at": now_ist()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Video not found")
    return AssignmentOut(video_id=video_id, properties=cleaned)
