"""Threads: app credentials, connecting an account, and the account's numbers.

Connecting is done server-side: the dashboard sends the authorization code from
Threads' redirect, and the server swaps it for a 60-day token with the app secret
it holds — the secret never reaches the browser. The token, the Threads user id
and the profile land on the channel doc.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_channel_platform, get_db, get_threads_oauth_config
from app.dependencies import verify_api_key
from app.services.channel_profile import build_profile_update, persist_profile_update
from app.services.publish_failure import platform_message
from app.services.threads import (
    AUTHORIZE_URL,
    SCOPES,
    ThreadsService,
    exchange_code,
    exchange_long_lived,
    token_doc,
)
from app.services.threads_token_policy import expires_at
from app.timezone import IST, assume_utc, now_ist

router = APIRouter(prefix="/api/v1/channels", tags=["threads"], dependencies=[Depends(verify_api_key)])


class ThreadsOAuthConfig(BaseModel):
    app_id: str = Field(..., min_length=1, description="The Threads app id (not the Facebook app id)")
    app_secret: str = Field(..., min_length=1, description="The Threads app secret")


class ThreadsConnect(BaseModel):
    code: str = Field(..., min_length=1)
    redirect_uri: str = Field(..., min_length=1, description="Exactly the redirect URI the authorize call used")


class ThreadsTokenStatus(BaseModel):
    connected: bool
    valid: bool | None = None
    username: str | None = None
    expires_at: str | None = None
    days_left: int | None = None
    error: str | None = None


class AudienceRow(BaseModel):
    value: str
    count: int


class ThreadsDay(BaseModel):
    date: str
    views: int | None = None
    followers_count: int | None = None


class ThreadsAudienceOut(BaseModel):
    followers_count: int | None = None
    days: list[ThreadsDay]
    country: list[AudienceRow] = []
    city: list[AudienceRow] = []
    age: list[AudienceRow] = []
    gender: list[AudienceRow] = []
    demographics_updated_at: str | None = None


class ThreadsPostOut(BaseModel):
    video_id: str
    threads_media_id: str
    title: str
    text: str
    permalink: str | None = None
    thumbnail_url: str | None = None
    media_type: str | None = None
    published_at: str | None = None
    views: int | None = None
    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    quotes: int | None = None
    shares: int | None = None


def _error_text(exc: Exception) -> str:
    return platform_message(exc) or str(exc)


async def _threads_channel(db: AsyncIOMotorDatabase, channel_id: str) -> dict[str, Any]:
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    if get_channel_platform(channel) != "threads":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a Threads channel")
    return channel


@router.put("/config/threads-oauth", tags=["config"])
async def set_threads_oauth_config(
    body: ThreadsOAuthConfig, db: AsyncIOMotorDatabase = Depends(get_db)
) -> dict[str, Any]:
    """Save the Threads app id and secret used to connect accounts."""
    await db.config.update_one(
        {"key": "threads_oauth"},
        {
            "$set": {
                "key": "threads_oauth",
                "app_id": body.app_id.strip(),
                "app_secret": body.app_secret.strip(),
                "updated_at": now_ist(),
            }
        },
        upsert=True,
    )
    return {"ok": True}


@router.get("/config/threads-oauth", tags=["config"])
async def get_threads_oauth(db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    """Whether Threads is set up, plus what the dashboard needs to start a login (never the secret)."""
    doc = await get_threads_oauth_config(db)
    if not doc:
        return {"configured": False}
    return {"configured": True, "app_id": doc["app_id"], "authorize_url": AUTHORIZE_URL, "scopes": list(SCOPES)}


@router.post("/{channel_id}/threads-connect")
async def connect_threads(
    channel_id: str, body: ThreadsConnect, db: AsyncIOMotorDatabase = Depends(get_db)
) -> dict[str, Any]:
    """Swap a Threads authorization code for a 60-day token and attach the account to this channel."""
    await _threads_channel(db, channel_id)
    config = await get_threads_oauth_config(db)
    if not config:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Threads app id and secret are not set yet")
    try:
        short = await asyncio.to_thread(
            exchange_code, config["app_id"], config["app_secret"], body.code, body.redirect_uri
        )
        long_lived = await asyncio.to_thread(exchange_long_lived, config["app_secret"], short["access_token"])
        profile = await asyncio.to_thread(ThreadsService(long_lived["access_token"]).get_profile)
    except requests.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Threads refused the login: {_error_text(exc)[:300]}"
        )

    user_id = str(profile.get("id") or short.get("user_id") or "")
    clash = await db.channels.find_one(
        {"threads_user_id": user_id, "channel_id": {"$ne": channel_id}}, {"channel_id": 1}
    )
    if clash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"@{profile.get('username')} is already connected to channel '{clash['channel_id']}'",
        )
    await db.channels.update_one(
        {"channel_id": channel_id},
        {
            "$set": {
                "threads_tokens": token_doc(long_lived, user_id),
                "threads_user_id": user_id,
                "updated_at": now_ist(),
            }
        },
    )
    channel = await db.channels.find_one({"channel_id": channel_id})
    await persist_profile_update(
        db, channel_id, build_profile_update("threads", profile, (channel or {}).get("name", ""))
    )
    return {"ok": True, "channel_id": channel_id, "username": profile.get("username"), "threads_user_id": user_id}


@router.get("/{channel_id}/threads-token/status", response_model=ThreadsTokenStatus)
async def threads_token_status(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> ThreadsTokenStatus:
    """Whether the channel's Threads login works right now, and when it expires."""
    channel = await _threads_channel(db, channel_id)
    tokens = channel.get("threads_tokens")
    if not tokens or not tokens.get("access_token"):
        return ThreadsTokenStatus(connected=False)
    expiry = expires_at(tokens)
    days_left = int((expiry - now_ist()).total_seconds() // 86400) if expiry else None
    try:
        profile = await asyncio.to_thread(ThreadsService(tokens["access_token"]).get_profile)
    except requests.HTTPError as exc:
        return ThreadsTokenStatus(
            connected=True,
            valid=False,
            expires_at=expiry.isoformat() if expiry else None,
            days_left=days_left,
            error=_error_text(exc)[:300],
        )
    return ThreadsTokenStatus(
        connected=True,
        valid=True,
        username=profile.get("username"),
        expires_at=expiry.isoformat() if expiry else None,
        days_left=days_left,
    )


@router.get("/{channel_id}/threads/audience", response_model=ThreadsAudienceOut)
async def threads_audience(
    channel_id: str, days: int = Query(90, ge=1, le=365), db: AsyncIOMotorDatabase = Depends(get_db)
) -> ThreadsAudienceOut:
    """Followers, views per day as Threads reports them, and follower demographics (from 100 followers)."""
    channel = await _threads_channel(db, channel_id)
    since = (now_ist().astimezone(IST).date() - timedelta(days=days - 1)).isoformat()
    rows = [
        ThreadsDay(date=d["date"], views=d.get("views"), followers_count=d.get("followers_count"))
        async for d in db.threads_account_daily.find({"channel_id": channel_id, "date": {"$gte": since}}).sort(
            "date", 1
        )
    ]
    audience = await db.threads_audience.find_one({"channel_id": channel_id}) or {}
    updated = audience.get("updated_at")
    return ThreadsAudienceOut(
        followers_count=channel.get("subscriber_count"),
        days=rows,
        country=audience.get("country") or [],
        city=audience.get("city") or [],
        age=audience.get("age") or [],
        gender=audience.get("gender") or [],
        demographics_updated_at=assume_utc(updated).isoformat() if isinstance(updated, datetime) else None,
    )


@router.get("/{channel_id}/threads/posts", response_model=list[ThreadsPostOut])
async def threads_posts(
    channel_id: str, limit: int = Query(100, ge=1, le=500), db: AsyncIOMotorDatabase = Depends(get_db)
) -> list[ThreadsPostOut]:
    """The channel's posts, newest first, with their latest counts."""
    await _threads_channel(db, channel_id)
    out: list[ThreadsPostOut] = []
    cursor = (
        db.videos.find({"channel_id": channel_id, "threads_media_id": {"$ne": None}, "status": "published"})
        .sort("published_at", -1)
        .limit(limit)
    )
    async for v in cursor:
        m = v.get("metadata") or {}
        published = v.get("published_at")
        out.append(
            ThreadsPostOut(
                video_id=v["video_id"],
                threads_media_id=v["threads_media_id"],
                title=v.get("title") or "",
                text=v.get("description") or "",
                permalink=v.get("threads_permalink"),
                thumbnail_url=v.get("thumbnail_url"),
                media_type=v.get("threads_media_type"),
                published_at=assume_utc(published).isoformat() if isinstance(published, datetime) else None,
                views=m.get("views"),
                likes=m.get("likes"),
                replies=m.get("comments"),
                reposts=m.get("reposts"),
                quotes=m.get("quotes"),
                shares=m.get("shares"),
            )
        )
    return out
