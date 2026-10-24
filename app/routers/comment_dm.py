"""Comment-to-DM rules, their log, and the webhook setup, for the dashboard.

A channel has an optional default rule that covers every reel, and any reel can have its own rule
that replaces it (or, switched off, silences it). See ``app/services/comment_dm.py`` for the rules
and ``comment_dm_worker.py`` for how the DMs go out.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import requests
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.config import get_settings
from app.database import get_db
from app.dependencies import verify_api_key
from app.services import comment_dm_store as store
from app.services import meta_webhook_config
from app.services.comment_dm import DmButton, DmRule, StoredRule
from app.services.instagram import graph_error_message
from app.services.instagram_tokens import PROVIDER_FACEBOOK, PROVIDER_INSTAGRAM, has_provider
from app.timezone import now_ist

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/comment-dm",
    tags=["comment-dm"],
    dependencies=[Depends(verify_api_key)],
)
config_router = APIRouter(prefix="/api/v1/comment-dm", tags=["comment-dm"], dependencies=[Depends(verify_api_key)])

# Instagram media ids are numeric; anything else is a mistake, not a reel.
MEDIA_ID = Path(pattern=r"^[0-9]{5,32}$")
# Our video ids are UUIDs.
VIDEO_ID = Path(pattern=r"^[A-Za-z0-9-]{8,64}$")
# The webhook fields we keep subscribed: comments drive the DMs; messages was already on and stays on.
WEBHOOK_FIELDS = ["comments", "messages"]
WEBHOOK_FIELDS_FIELD = "instagram_webhook_fields"


class ReelRuleOut(BaseModel):
    media_id: str
    title: str | None = None
    stored: StoredRule


class VideoRuleOut(BaseModel):
    """A rule set on one of our videos — usually while it was still scheduled."""

    video_id: str
    title: str | None = None
    status: str | None = None
    scheduled_at: datetime | None = None
    # Set once the reel has published; the rule then answers its comments.
    media_id: str | None = None
    stored: StoredRule


class ScheduledVideoOut(BaseModel):
    video_id: str
    title: str | None = None
    scheduled_at: datetime | None = None


class OverviewOut(BaseModel):
    channel_id: str
    default: StoredRule | None
    reels: list[ReelRuleOut]
    videos: list[VideoRuleOut]
    # Sending needs an Instagram Login token; reading comments for the poll needs a Facebook Login one.
    can_send: bool
    can_poll: bool
    webhook_fields: list[str]


class EventOut(BaseModel):
    comment_id: str
    media_id: str
    title: str | None = None
    username: str = ""
    text: str = ""
    trigger: str = ""
    source: str = ""
    status: str
    skip_reason: str | None = None
    error: str | None = None
    reply_error: str | None = None
    dm_text: str = ""
    buttons: list[DmButton] = []
    # "text", "buttons", or "text_fallback" when Instagram refused the buttons and the links went as text.
    dm_format: str | None = None
    button_error: str | None = None
    dm_message_id: str | None = None
    reply_text: str | None = None
    reply_comment_id: str | None = None
    commented_at: datetime | None = None
    created_at: datetime | None = None
    send_after: datetime | None = None
    sent_at: datetime | None = None


class DmCounts(BaseModel):
    comments: int
    dms_sent: int
    replies_posted: int
    queued: int
    failed: int
    skipped: int


class SummaryOut(BaseModel):
    last_24h: DmCounts
    all_time: DmCounts


class SubscribeOut(BaseModel):
    account_id: str
    fields: list[str]


class WebhookConfigOut(BaseModel):
    callback_url: str
    verify_token: str
    has_app_secret: bool


class WebhookConfigIn(BaseModel):
    app_secret: str = Field(min_length=16, max_length=128)


async def _channel(db: AsyncIOMotorDatabase, channel_id: str) -> dict[str, Any]:
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(404, "Channel not found")
    if channel.get("platform") != "instagram":
        raise HTTPException(422, "Comment-to-DM is Instagram only")
    return dict(channel)


async def _titles(db: AsyncIOMotorDatabase, channel_id: str, media_ids: list[str]) -> dict[str, str]:
    cursor = db.videos.find(
        {"channel_id": channel_id, "instagram_media_id": {"$in": media_ids}}, {"instagram_media_id": 1, "title": 1}
    )
    return {str(v["instagram_media_id"]): str(v.get("title") or "") async for v in cursor}


@router.get("", response_model=OverviewOut)
async def overview(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> OverviewOut:
    channel = await _channel(db, channel_id)
    reel_rules = await store.list_reel_rules(db, channel_id)
    titles = await _titles(db, channel_id, list(reel_rules))
    video_rules = await store.list_video_rules(db, channel_id)
    video_docs = {
        str(v["video_id"]): v
        async for v in db.videos.find(
            {"channel_id": channel_id, "video_id": {"$in": list(video_rules)}},
            {"video_id": 1, "title": 1, "status": 1, "scheduled_at": 1, "instagram_media_id": 1},
        )
    }
    videos = [
        VideoRuleOut(
            video_id=vid,
            title=video_docs.get(vid, {}).get("title") or None,
            status=video_docs.get(vid, {}).get("status"),
            scheduled_at=video_docs.get(vid, {}).get("scheduled_at"),
            media_id=video_docs.get(vid, {}).get("instagram_media_id") or None,
            stored=s,
        )
        for vid, s in video_rules.items()
    ]
    return OverviewOut(
        channel_id=channel_id,
        default=await store.load_default(db, channel_id),
        reels=[ReelRuleOut(media_id=mid, title=titles.get(mid) or None, stored=s) for mid, s in reel_rules.items()],
        videos=videos,
        can_send=has_provider(channel, PROVIDER_INSTAGRAM),
        can_poll=has_provider(channel, PROVIDER_FACEBOOK),
        webhook_fields=[str(f) for f in channel.get(WEBHOOK_FIELDS_FIELD) or []],
    )


@router.put("/default", response_model=StoredRule)
async def put_default(channel_id: str, rule: DmRule, db: AsyncIOMotorDatabase = Depends(get_db)) -> StoredRule:
    await _channel(db, channel_id)
    return await store.save_default(db, channel_id, rule, now_ist())


# Deletes answer with a body rather than 204: the dashboard's fetch helper always parses JSON.
@router.delete("/default")
async def delete_default(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, bool]:
    await _channel(db, channel_id)
    await store.clear_default(db, channel_id)
    return {"deleted": True}


@router.put("/reels/{media_id}", response_model=StoredRule)
async def put_reel_rule(
    channel_id: str, rule: DmRule, media_id: str = MEDIA_ID, db: AsyncIOMotorDatabase = Depends(get_db)
) -> StoredRule:
    await _channel(db, channel_id)
    return await store.save_reel_rule(db, channel_id, media_id, rule, now_ist())


@router.delete("/reels/{media_id}")
async def delete_reel_rule(
    channel_id: str, media_id: str = MEDIA_ID, db: AsyncIOMotorDatabase = Depends(get_db)
) -> dict[str, bool]:
    await _channel(db, channel_id)
    await store.clear_reel_rule(db, channel_id, media_id)
    return {"deleted": True}


async def _video(db: AsyncIOMotorDatabase, channel_id: str, video_id: str) -> None:
    if not await db.videos.find_one({"channel_id": channel_id, "video_id": video_id}, {"_id": 1}):
        raise HTTPException(404, "Video not found on this channel")


@router.get("/scheduled-videos", response_model=list[ScheduledVideoOut])
async def scheduled_videos(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> list[ScheduledVideoOut]:
    """Reels queued to post, soonest first, so a rule can be ready before the first comment."""
    await _channel(db, channel_id)
    cursor = db.videos.find(
        {"channel_id": channel_id, "status": "queued"}, {"video_id": 1, "title": 1, "scheduled_at": 1}
    ).sort("scheduled_at", 1)
    return [ScheduledVideoOut.model_validate(v) for v in await cursor.limit(100).to_list(100)]


@router.put("/videos/{video_id}", response_model=StoredRule)
async def put_video_rule(
    channel_id: str, rule: DmRule, video_id: str = VIDEO_ID, db: AsyncIOMotorDatabase = Depends(get_db)
) -> StoredRule:
    await _channel(db, channel_id)
    await _video(db, channel_id, video_id)
    return await store.save_video_rule(db, channel_id, video_id, rule, now_ist())


@router.delete("/videos/{video_id}")
async def delete_video_rule(
    channel_id: str, video_id: str = VIDEO_ID, db: AsyncIOMotorDatabase = Depends(get_db)
) -> dict[str, bool]:
    await _channel(db, channel_id)
    await store.clear_video_rule(db, channel_id, video_id)
    return {"deleted": True}


@router.get("/events", response_model=list[EventOut])
async def events(
    channel_id: str,
    media_id: str | None = Query(None, pattern=r"^[0-9]{5,32}$"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> list[EventOut]:
    await _channel(db, channel_id)
    found = await store.list_events(db, channel_id, media_id, limit)
    titles = await _titles(db, channel_id, sorted({str(e["media_id"]) for e in found}))
    return [EventOut.model_validate({**e, "title": titles.get(str(e["media_id"])) or None}) for e in found]


@router.get("/summary", response_model=SummaryOut)
async def summary(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> SummaryOut:
    await _channel(db, channel_id)
    return SummaryOut(
        last_24h=DmCounts(**await store.summary(db, channel_id, now_ist() - timedelta(hours=24))),
        all_time=DmCounts(**await store.summary(db, channel_id, None)),
    )


@router.post("/subscribe", response_model=SubscribeOut)
async def subscribe(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> SubscribeOut:
    """Turn on Meta's comment webhook for this account. Instagram Login only: on Facebook Login it
    needs ``pages_manage_metadata``, which the shared token doesn't hold."""
    import app.main as main_mod

    await _channel(db, channel_id)
    manager = main_mod.instagram_service_manager
    resolved = await manager.get_service_and_user_id(channel_id, PROVIDER_INSTAGRAM) if manager else None
    if resolved is None or resolved[0].provider != PROVIDER_INSTAGRAM:
        raise HTTPException(422, "Webhooks need an Instagram Login token on this channel")
    service = resolved[0]
    try:
        current = await asyncio.to_thread(service.subscribed_webhook_fields)
        wanted = sorted(set(current) | set(WEBHOOK_FIELDS))
        await asyncio.to_thread(service.subscribe_webhooks, wanted)
        fields = await asyncio.to_thread(service.subscribed_webhook_fields)
        account_id = await asyncio.to_thread(service.account_user_id)
    except requests.RequestException as exc:
        raise HTTPException(502, f"Instagram refused: {graph_error_message(exc)[:300]}") from exc
    await db.channels.update_one(
        {"channel_id": channel_id},
        {"$set": {store.WEBHOOK_ID_FIELD: account_id, WEBHOOK_FIELDS_FIELD: fields}},
    )
    return SubscribeOut(account_id=account_id, fields=fields)


@config_router.get("/webhook-config", response_model=WebhookConfigOut)
async def get_webhook_config(db: AsyncIOMotorDatabase = Depends(get_db)) -> WebhookConfigOut:
    """What to paste into the Meta app's webhook settings. The secret itself is never returned."""
    return WebhookConfigOut(
        callback_url=f"{get_settings().PUBLIC_API_URL}{meta_webhook_config.WEBHOOK_PATH}",
        verify_token=await meta_webhook_config.verify_token(db),
        has_app_secret=await meta_webhook_config.app_secret(db) is not None,
    )


@config_router.put("/webhook-config", response_model=WebhookConfigOut)
async def put_webhook_config(body: WebhookConfigIn, db: AsyncIOMotorDatabase = Depends(get_db)) -> WebhookConfigOut:
    await meta_webhook_config.set_app_secret(db, body.app_secret.strip())
    return await get_webhook_config(db)
