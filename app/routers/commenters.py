"""Commenter stats for the Comments area: who comments on an Instagram channel, and how often."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
from app.services import comment_log
from app.services.commenter_stats import WINDOWS, account_detail, commenter_stats
from app.timezone import IST, assume_utc, now_ist

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/commenters",
    tags=["commenters"],
    dependencies=[Depends(verify_api_key)],
)

SYNC_COOLDOWN = timedelta(minutes=2)
_syncing: set[str] = set()

_ROW_FIELDS = {
    "_id": 0,
    "comment_id": 1,
    "author": 1,
    "username": 1,
    "media_id": 1,
    "is_reply": 1,
    "text": 1,
    "commented_at": 1,
}
_VIEWERS = {"own": {"$ne": True}}


async def _channel(db: AsyncIOMotorDatabase, channel_id: str) -> dict[str, Any]:
    channel: dict[str, Any] | None = await db.channels.find_one({"channel_id": channel_id})
    if channel is None:
        raise HTTPException(404, "Channel not found")
    return channel


def _synced_at(channel: dict[str, Any]) -> str | None:
    synced = channel.get(comment_log.SYNCED_FIELD)
    return assume_utc(synced).astimezone(IST).isoformat() if isinstance(synced, datetime) else None


@router.get("")
async def commenters(
    channel_id: str,
    window: Literal["7d", "30d", "90d", "all"] = Query("30d"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """How many accounts commented, the top ones (comments, top-level vs replies, reels), repeat and new
    commenters, and a daily series. Worked out from the comment log on every call, so it's live."""
    channel = await _channel(db, channel_id)
    rows = await db.comment_log.find({"channel_id": channel_id, **_VIEWERS}, _ROW_FIELDS).to_list(length=None)
    return {
        "window": window,
        "synced_at": _synced_at(channel),
        **commenter_stats(rows, now_ist(), WINDOWS[window]),
    }


@router.get("/accounts/{author}")
async def commenter_account(channel_id: str, author: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict:
    """Everything one account has said on this channel, newest first."""
    await _channel(db, channel_id)
    rows = await db.comment_log.find(
        {"channel_id": channel_id, "author": author.lower(), **_VIEWERS}, _ROW_FIELDS
    ).to_list(length=None)
    media_ids = list({r["media_id"] for r in rows})
    titles = {
        str(v["instagram_media_id"]): str(v.get("title") or "")
        async for v in db.videos.find(
            {"channel_id": channel_id, "instagram_media_id": {"$in": media_ids}}, {"instagram_media_id": 1, "title": 1}
        )
    }
    detail = account_detail(rows, titles)
    if detail is None:
        raise HTTPException(404, "No comments from this account")
    return detail


@router.post("/sync")
async def sync_now(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    """Read comments back from Instagram now instead of waiting for the next scheduled sync."""
    import app.main as main_mod

    channel = await _channel(db, channel_id)
    if channel.get("platform") != "instagram":
        raise HTTPException(422, "Commenter stats are Instagram-only")
    now = now_ist()
    synced = channel.get(comment_log.SYNCED_FIELD)
    if isinstance(synced, datetime) and now - assume_utc(synced) < SYNC_COOLDOWN:
        raise HTTPException(429, "Synced under 2 minutes ago")
    if channel_id in _syncing:
        raise HTTPException(409, "A sync is already running")
    manager = main_mod.instagram_service_manager
    resolved = await manager.get_service_and_user_id(channel_id) if manager else None
    if resolved is None:
        raise HTTPException(422, "No Instagram token on this channel")
    _syncing.add(channel_id)
    try:
        return await comment_log.sync_channel(db, channel, resolved[0], resolved[1], now)
    finally:
        _syncing.discard(channel_id)
