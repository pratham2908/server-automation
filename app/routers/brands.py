"""Brand lookups for Sales: a brand's public Instagram profile and recent posts, read through business_discovery.

Read-only. One Graph call per brand, kept for a day. Only Business and Creator accounts can be read.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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
from app.timezone import assume_utc

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/brands",
    tags=["brands"],
    dependencies=[Depends(verify_api_key)],
)

# Pause between brands in a batch: well inside Graph's limits, and it keeps one batch from bursting.
PACE_SECONDS = 0.25
_cache = bl.LookupCache()
_guard = bl.CallGuard()
# Usage readings taken in worker threads, written to Mongo by the request that made them.
_pending_usage: list[dict[str, Any]] = []


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


async def _warm_from_db(db: AsyncIOMotorDatabase, raws: list[str], media_limit: int) -> None:
    """Reload lookups saved before a restart, so a deploy does not spend the hourly budget on repeats."""
    now = datetime.now(UTC)
    for raw in raws:
        handle = bl.clean_handle(raw)
        if handle is None or _cache.get(handle, media_limit) is not None:
            continue
        doc = await db.brand_lookup_cache.find_one({"handle": handle, "media_limit": media_limit})
        if doc:
            age = (now - assume_utc(doc["fetched_at"])).total_seconds()
            _cache.put(handle, media_limit, doc["result"], age=age)


async def _save_to_db(db: AsyncIOMotorDatabase, results: list[dict[str, Any]], media_limit: int) -> None:
    now = datetime.now(UTC)
    for r in results:
        if r["status"] == "ok" and not r.get("cached"):
            body = {k: v for k, v in r.items() if k != "cached"}
            await db.brand_lookup_cache.update_one(
                {"handle": r["handle"], "media_limit": media_limit},
                {"$set": {"result": body, "fetched_at": now}},
                upsert=True,
            )


def _note_usage(service: InstagramService) -> None:
    percent = service.last_app_usage
    _guard.record_usage(percent)
    if percent is not None:
        _pending_usage.append({"at": datetime.now(UTC), "percent": percent})


async def _flush_usage(db: AsyncIOMotorDatabase) -> None:
    rows = list(_pending_usage)
    del _pending_usage[: len(rows)]
    if rows:
        await db.brand_lookup_usage.insert_many(rows)


def _lookup_one(service: InstagramService, ig_user_id: str, raw: str, media_limit: int) -> dict[str, Any]:
    handle = bl.clean_handle(raw)
    if handle is None:
        return bl.failure(raw.strip()[:60], "invalid_handle", "Not a valid Instagram handle")
    cached = _cache.get(handle, media_limit)
    if cached is not None:
        return {**cached, "cached": True}
    refused = _guard.refusal()
    if refused:
        return bl.failure(handle, "rate_limited", refused)
    _guard.record_call()
    try:
        reply = service.business_discovery(ig_user_id, bl.discovery_fields(handle, media_limit))
        _note_usage(service)
    except requests.HTTPError as exc:
        _note_usage(service)
        message = graph_error_message(exc)
        status = exc.response.status_code if exc.response is not None else 0
        if bl.is_app_limit(message):
            _guard.record_app_limit()
            return bl.failure(handle, "rate_limited", message[:300])
        # Instagram answers 400 for an account that is personal or does not exist; anything else is ours to report.
        kind: bl.Status = "not_business" if status == 400 else "error"
        logger.info("business_discovery for %s refused: %s", handle, message[:200])
        return bl.failure(handle, kind, message[:300])
    except requests.RequestException as exc:
        return bl.failure(handle, "error", str(exc)[:300])
    result = bl.shape(handle, reply)
    _cache.put(handle, media_limit, result)
    return {**result, "cached": False}


@router.get("/usage/summary")
async def usage_summary(
    channel_id: str, hours: int = Query(24, ge=1, le=168), db: AsyncIOMotorDatabase = Depends(get_db)
) -> dict[str, Any]:
    """How much of the app's Graph limit (x-app-usage, percent) the lookups saw, per hour, newest first."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = await db.brand_lookup_usage.find({"at": {"$gte": since}}).sort("at", 1).to_list(length=None)
    per_hour: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r["at"].strftime("%Y-%m-%dT%H:00Z")
        slot = per_hour.setdefault(key, {"hour": key, "calls": 0, "max_percent": 0.0})
        slot["calls"] += 1
        slot["max_percent"] = max(slot["max_percent"], r["percent"])
    return {
        "stop_percent": bl.USAGE_STOP_PERCENT,
        "samples": len(rows),
        "latest_percent": rows[-1]["percent"] if rows else None,
        "max_percent": max((r["percent"] for r in rows), default=None),
        "per_hour": sorted(per_hour.values(), key=lambda x: x["hour"], reverse=True),
    }


@router.get("/{handle}", response_model=BrandOut)
async def get_brand(
    channel_id: str,
    handle: str,
    media_limit: int = Query(30, ge=1, le=bl.MEDIA_LIMIT_MAX),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """One brand. A brand that cannot be read comes back 200 with status not_business / invalid_handle / error."""
    service, ig_user_id = await _service(db, channel_id)
    await _warm_from_db(db, [handle], media_limit)
    result = await asyncio.to_thread(_lookup_one, service, ig_user_id, handle, media_limit)
    await _flush_usage(db)
    await _save_to_db(db, [result], media_limit)
    return result


@router.post("/batch", response_model=list[BrandOut])
async def get_brands(
    channel_id: str, body: BatchIn, db: AsyncIOMotorDatabase = Depends(get_db)
) -> list[dict[str, Any]]:
    """Up to 50 brands, one result per handle in the order given, paced one call at a time."""
    service, ig_user_id = await _service(db, channel_id)
    await _warm_from_db(db, body.handles, body.media_limit)
    out: list[dict[str, Any]] = []
    for raw in body.handles:
        result = await asyncio.to_thread(_lookup_one, service, ig_user_id, raw, body.media_limit)
        out.append(result)
        if not result.get("cached") and result["status"] not in ("invalid_handle", "rate_limited"):
            await asyncio.sleep(PACE_SECONDS)
    await _flush_usage(db)
    await _save_to_db(db, out, body.media_limit)
    return out
