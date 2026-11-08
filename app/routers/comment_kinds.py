"""Comment kinds: every comment sorted into one kind (appreciation, humour, request, …), per channel.

The kinds are read in the same AI call as the channel's collectors (``comment_collector_worker``); see
``comment_collectors.KINDS`` for the list.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key
from app.services import comment_collector_store as store
from app.services import comment_collectors as cc
from app.timezone import now_ist, parse_ist, to_ist_iso

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/comment-kinds",
    tags=["comment-kinds"],
    dependencies=[Depends(verify_api_key)],
)


class KindsSettingIn(BaseModel):
    enabled: bool
    # When switching on: also sort the comments of the last N days.
    read_back_days: int = Field(default=0, ge=0, le=store.MAX_KIND_READ_BACK_DAYS)


async def _channel(db: AsyncIOMotorDatabase, channel_id: str) -> None:
    if not await db.channels.find_one({"channel_id": channel_id}, {"_id": 1}):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Channel not found")


def _when(value: str | None, name: str) -> datetime | None:
    if not value:
        return None
    try:
        return parse_ist(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"{name} is not a date") from exc


def _setting_out(setting: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": setting["enabled"],
        "since": to_ist_iso(setting.get("since")),
        "kinds": [{"kind": k, "label": label, "description": desc} for k, label, desc in cc.KINDS],
    }


@router.get("/settings")
async def get_setting(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    await _channel(db, channel_id)
    return _setting_out(await store.kinds_setting(db, channel_id))


@router.put("/settings")
async def put_setting(
    channel_id: str, body: KindsSettingIn, db: AsyncIOMotorDatabase = Depends(get_db)
) -> dict[str, Any]:
    await _channel(db, channel_id)
    return _setting_out(await store.set_kinds(db, channel_id, body.enabled, body.read_back_days, now_ist()))


async def _reel_titles(db: AsyncIOMotorDatabase, channel_id: str, media_ids: list[str]) -> dict[str, dict[str, Any]]:
    videos = await db.videos.find(
        {"channel_id": channel_id, "instagram_media_id": {"$in": media_ids}},
        {"instagram_media_id": 1, "title": 1, "published_at": 1},
    ).to_list(None)
    return {
        str(v.get("instagram_media_id")): {
            "title": str(v.get("title") or "").split("\n")[0][:120],
            "published_at": to_ist_iso(v.get("published_at")),
        }
        for v in videos
    }


@router.get("")
async def kinds_summary(
    channel_id: str,
    media_id: str | None = Query(None, description="Only this reel's comments"),
    since: str | None = Query(None, description="Comments written on or after this time (ISO; bare is IST)"),
    until: str | None = Query(None),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """Each kind's comments, share, people, likes and most liked comment; and the mix on each reel."""
    await _channel(db, channel_id)
    summary = await store.kinds_summary(
        db, channel_id, media_id=media_id, since=_when(since, "since"), until=_when(until, "until")
    )
    titles = await _reel_titles(db, channel_id, [r["media_id"] for r in summary["reels"]])
    reels = [{**r, **titles.get(r["media_id"], {"title": "", "published_at": None})} for r in summary["reels"]]
    reels.sort(key=lambda r: r["published_at"] or "", reverse=True)
    return {**summary, "reels": reels, "setting": _setting_out(await store.kinds_setting(db, channel_id))}


@router.get("/comments")
async def kind_comments(
    channel_id: str,
    kind: str | None = Query(None, description="Only comments of this kind"),
    media_id: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    sort: Literal["likes", "recent"] = Query("likes"),
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> list[dict[str, Any]]:
    await _channel(db, channel_id)
    if kind and kind not in cc.KIND_IDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"unknown kind {kind!r}")
    rows = await store.kind_comments(
        db,
        channel_id,
        kind=kind,
        media_id=media_id,
        since=_when(since, "since"),
        until=_when(until, "until"),
        sort=sort,
        limit=limit,
    )
    return [
        {
            "comment_id": r.get("comment_id"),
            "media_id": r.get("media_id"),
            "username": r.get("username"),
            "text": r.get("text"),
            "kind": r.get("kind"),
            "like_count": r.get("like_count"),
            "comment_gone": bool(r.get("comment_gone")),
            "commented_at": to_ist_iso(r.get("commented_at")),
        }
        for r in rows
    ]
