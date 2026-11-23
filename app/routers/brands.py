"""Brand lookups for Sales: a brand's public Instagram profile and recent posts, read through business_discovery.

Read-only. One Graph call per brand, kept for a day. Only Business and Creator accounts can be read.
"""

from __future__ import annotations

import asyncio
from typing import Any

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger
from app.services import brand_lookup as bl
from app.services.instagram import InstagramService, graph_error_message
from app.services.instagram_tokens import PROVIDER_FACEBOOK

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/brands",
    tags=["brands"],
    dependencies=[Depends(verify_api_key)],
)

# Pause between brands in a batch: well inside Graph's limits, and it keeps one batch from bursting.
PACE_SECONDS = 0.25
_cache = bl.LookupCache()


class MediaOut(BaseModel):
    caption: str | None = None
    permalink: str | None = None
    timestamp: str | None = None
    like_count: int | None = Field(None, description="None when the brand hides likes")
    comments_count: int | None = None
    media_type: str | None = None


class BrandOut(BaseModel):
    handle: str
    status: bl.Status
    username: str | None = None
    name: str | None = None
    biography: str | None = None
    website: str | None = None
    followers_count: int | None = None
    media_count: int | None = None
    media: list[MediaOut] = []
    error: str | None = Field(None, description="Instagram's own words when status is not ok")
    cached: bool = False


class BatchIn(BaseModel):
    handles: list[str] = Field(min_length=1, max_length=bl.MAX_BATCH)
    media_limit: int = Field(30, ge=1, le=bl.MEDIA_LIMIT_MAX)


async def _service(db: AsyncIOMotorDatabase, channel_id: str) -> tuple[InstagramService, str]:
    import app.main as main_mod

    channel = await db.channels.find_one({"channel_id": channel_id}, {"_id": 1})
    if channel is None:
        raise HTTPException(404, "Channel not found")
    manager = main_mod.instagram_service_manager
    resolved = await manager.get_service_and_user_id(channel_id, PROVIDER_FACEBOOK) if manager else None
    if resolved is None:
        raise HTTPException(422, "This channel has no Instagram token")
    return resolved


def _lookup_one(service: InstagramService, ig_user_id: str, raw: str, media_limit: int) -> dict[str, Any]:
    handle = bl.clean_handle(raw)
    if handle is None:
        return bl.failure(raw.strip()[:60], "invalid_handle", "Not a valid Instagram handle")
    cached = _cache.get(handle, media_limit)
    if cached is not None:
        return {**cached, "cached": True}
    try:
        reply = service.business_discovery(ig_user_id, bl.discovery_fields(handle, media_limit))
    except requests.HTTPError as exc:
        message = graph_error_message(exc)
        status = exc.response.status_code if exc.response is not None else 0
        # Instagram answers 400 for an account that is personal or does not exist; anything else is ours to report.
        kind: bl.Status = "not_business" if status == 400 else "error"
        logger.info("business_discovery for %s refused: %s", handle, message[:200])
        return bl.failure(handle, kind, message[:300])
    except requests.RequestException as exc:
        return bl.failure(handle, "error", str(exc)[:300])
    result = bl.shape(handle, reply)
    _cache.put(handle, media_limit, result)
    return {**result, "cached": False}


@router.get("/{handle}", response_model=BrandOut)
async def get_brand(
    channel_id: str,
    handle: str,
    media_limit: int = Query(30, ge=1, le=bl.MEDIA_LIMIT_MAX),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """One brand. A brand that cannot be read comes back 200 with status not_business / invalid_handle / error."""
    service, ig_user_id = await _service(db, channel_id)
    return await asyncio.to_thread(_lookup_one, service, ig_user_id, handle, media_limit)


@router.post("/batch", response_model=list[BrandOut])
async def get_brands(
    channel_id: str, body: BatchIn, db: AsyncIOMotorDatabase = Depends(get_db)
) -> list[dict[str, Any]]:
    """Up to 50 brands, one result per handle in the order given, paced one call at a time."""
    service, ig_user_id = await _service(db, channel_id)
    out: list[dict[str, Any]] = []
    for raw in body.handles:
        result = await asyncio.to_thread(_lookup_one, service, ig_user_id, raw, body.media_limit)
        out.append(result)
        if not result.get("cached") and result["status"] != "invalid_handle":
            await asyncio.sleep(PACE_SECONDS)
    return out
