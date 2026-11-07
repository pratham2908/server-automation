"""What a DM can carry beyond text: uploaded media, the account's ice breakers and chat menu, and the
log of replies our buttons have sent.

Ice breakers and the menu are SAVED here and only reach Instagram when Publish is pressed: they change
what every person opening a chat with the account sees, which is not something a draft should do.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

import requests
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import verify_api_key
from app.services import dm_replies, meet_city
from app.services.dm_content import InboxSettings, MediaType, ice_breakers_body, menu_body, settings_replies
from app.services.dm_sender import r2_signer
from app.services.instagram import InstagramService, graph_error_message
from app.services.instagram_tokens import PROVIDER_INSTAGRAM
from app.timezone import assume_utc, now_ist, to_ist_iso

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/instagram/dm-setup",
    tags=["instagram-dms"],
    dependencies=[Depends(verify_api_key)],
)

SETTINGS_FIELD = "dm_inbox_settings"
PUBLISHED_FIELD = "dm_inbox_published_at"

# What Instagram's Send API takes as an attachment, and how big (its documented limits).
MEDIA_TYPES: dict[str, tuple[MediaType, str]] = {
    "image/jpeg": ("image", "jpg"),
    "image/png": ("image", "png"),
    "image/gif": ("image", "gif"),
    "video/mp4": ("video", "mp4"),
    "video/quicktime": ("video", "mov"),
    "video/webm": ("video", "webm"),
    "audio/mpeg": ("audio", "mp3"),
    "audio/mp4": ("audio", "m4a"),
    "audio/x-m4a": ("audio", "m4a"),
    "audio/aac": ("audio", "aac"),
    "audio/wav": ("audio", "wav"),
    "audio/x-wav": ("audio", "wav"),
}
MAX_BYTES: dict[MediaType, int] = {"image": 8 * 1024 * 1024, "video": 25 * 1024 * 1024, "audio": 25 * 1024 * 1024}
CHUNK = 1024 * 1024


class MediaOut(BaseModel):
    type: MediaType
    r2_key: str
    # A link to show the file in the dashboard; it expires, the key does not.
    preview_url: str


class SettingsOut(BaseModel):
    settings: InboxSettings
    published_at: str | None = None
    updated_at: str | None = None


class LiveOut(BaseModel):
    # Exactly what Instagram holds now, as it returns it.
    ice_breakers: list[dict[str, Any]]
    persistent_menu: list[dict[str, Any]]


class PublishOut(BaseModel):
    ice_breakers: bool
    menu: bool
    published_at: str


class OutboxItemOut(BaseModel):
    key: str
    recipient_id: str
    reason: str
    status: str
    error: str | None = None
    follows: bool | None = None
    gated: bool = False
    text: str = ""
    created_at: str | None = None
    sent_at: str | None = None


async def _channel(db: AsyncIOMotorDatabase, channel_id: str) -> dict[str, Any]:
    channel: dict[str, Any] | None = await db.channels.find_one({"channel_id": channel_id})
    if channel is None:
        raise HTTPException(404, "Channel not found")
    if channel.get("platform") != "instagram":
        raise HTTPException(422, "DMs are Instagram only")
    return channel


async def _service(channel_id: str) -> tuple[InstagramService, str]:
    import app.main as main_mod

    manager = main_mod.instagram_service_manager
    resolved = await manager.get_service_and_user_id(channel_id, PROVIDER_INSTAGRAM) if manager else None
    if resolved is None or resolved[0].provider != PROVIDER_INSTAGRAM:
        # Ice breakers and the menu are set through Instagram Login; the shared Facebook token can't.
        raise HTTPException(422, "This needs an Instagram Login token on the channel")
    return resolved


def _iso(value: Any) -> str | None:
    return to_ist_iso(assume_utc(value)) if isinstance(value, datetime) else None


@router.post("/media", response_model=MediaOut)
async def upload_media(
    channel_id: str, file: UploadFile = File(...), db: AsyncIOMotorDatabase = Depends(get_db)
) -> MediaOut:
    """Keep a picture, video or voice note for a DM. Sent by a fresh signed link each time it goes out."""
    import app.main as main_mod

    await _channel(db, channel_id)
    r2 = main_mod.r2_service
    signer = r2_signer()
    if r2 is None or signer is None:
        raise HTTPException(503, "File storage isn't configured on this server")
    known = MEDIA_TYPES.get((file.content_type or "").lower())
    if known is None:
        raise HTTPException(415, f"Instagram can't send a {file.content_type or 'file of unknown type'} in a DM")
    kind, ext = known
    # Read in chunks so an oversized upload is refused at the limit, not after it is all in memory.
    size = 0
    while chunk := await file.read(CHUNK):
        size += len(chunk)
        if size > MAX_BYTES[kind]:
            raise HTTPException(413, f"A {kind} can be at most {MAX_BYTES[kind] // (1024 * 1024)} MB")
    await file.seek(0)
    key = f"dm-media/{channel_id}/{uuid.uuid4().hex}.{ext}"
    await asyncio.to_thread(r2.upload_video, file.file, key)
    return MediaOut(type=kind, r2_key=key, preview_url=signer(key))


@router.get("/settings", response_model=SettingsOut)
async def get_settings(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> SettingsOut:
    channel = await _channel(db, channel_id)
    stored = channel.get(SETTINGS_FIELD) or {}
    return SettingsOut(
        settings=InboxSettings.model_validate(stored.get("settings") or {}),
        published_at=_iso(channel.get(PUBLISHED_FIELD)),
        updated_at=_iso(stored.get("updated_at")),
    )


@router.put("/settings", response_model=SettingsOut)
async def put_settings(
    channel_id: str, settings: InboxSettings, db: AsyncIOMotorDatabase = Depends(get_db)
) -> SettingsOut:
    """Save the ice breakers and menu here. Instagram is unchanged until they are published."""
    channel = await _channel(db, channel_id)
    now = now_ist()
    await db.channels.update_one(
        {"channel_id": channel_id},
        {"$set": {SETTINGS_FIELD: {"settings": settings.model_dump(mode="json"), "updated_at": now}}},
    )
    return SettingsOut(settings=settings, published_at=_iso(channel.get(PUBLISHED_FIELD)), updated_at=_iso(now))


@router.get("/settings/live", response_model=LiveOut)
async def live_settings(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> LiveOut:
    """What Instagram shows now, read from Instagram (one call)."""
    await _channel(db, channel_id)
    service, ig_user_id = await _service(channel_id)
    try:
        live = await asyncio.to_thread(service.get_messenger_profile, ig_user_id)
    except requests.RequestException as exc:
        raise HTTPException(502, f"Instagram refused: {graph_error_message(exc)[:300]}") from exc
    return LiveOut(
        ice_breakers=list(live.get("ice_breakers") or []), persistent_menu=list(live.get("persistent_menu") or [])
    )


@router.post("/settings/publish", response_model=PublishOut)
async def publish_settings(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> PublishOut:
    """Put the saved ice breakers and menu on the live account. An empty list clears that one there."""
    channel = await _channel(db, channel_id)
    stored = (channel.get(SETTINGS_FIELD) or {}).get("settings")
    if stored is None:
        raise HTTPException(409, "Nothing saved to publish yet")
    settings = InboxSettings.model_validate(stored)
    service, ig_user_id = await _service(channel_id)
    now = now_ist()
    # Registered first, so a tap the moment they go live finds its reply.
    await dm_replies.register(db, channel_id, settings_replies(settings), now)
    try:
        # Menu first: the menu post has been seen to wipe the ice breakers (2026-11-04), so the
        # ice breakers go up last.
        if settings.menu:
            await asyncio.to_thread(service.set_messenger_profile, ig_user_id, menu_body(settings, channel_id))
        else:
            await asyncio.to_thread(service.clear_messenger_profile, ig_user_id, ["persistent_menu"])
        if settings.ice_breakers:
            await asyncio.to_thread(service.set_messenger_profile, ig_user_id, ice_breakers_body(settings, channel_id))
        else:
            await asyncio.to_thread(service.clear_messenger_profile, ig_user_id, ["ice_breakers"])
        # Instagram has answered "success" to a set that was not live afterwards, so trust only
        # what reads back, and set a missing one once more before giving up.
        live = await asyncio.to_thread(service.get_messenger_profile, ig_user_id)
        if settings.ice_breakers and not live.get("ice_breakers"):
            await asyncio.to_thread(service.set_messenger_profile, ig_user_id, ice_breakers_body(settings, channel_id))
        if settings.menu and not live.get("persistent_menu"):
            await asyncio.to_thread(service.set_messenger_profile, ig_user_id, menu_body(settings, channel_id))
        live = await asyncio.to_thread(service.get_messenger_profile, ig_user_id)
    except requests.RequestException as exc:
        raise HTTPException(502, f"Instagram refused: {graph_error_message(exc)[:300]}") from exc
    missing = [
        name
        for name, wanted in (("ice breakers", settings.ice_breakers), ("menu", settings.menu))
        if wanted and not live.get("ice_breakers" if name == "ice breakers" else "persistent_menu")
    ]
    if missing:
        raise HTTPException(502, f"Instagram said yes but the {' and '.join(missing)} are not live when read back")
    await db.channels.update_one({"channel_id": channel_id}, {"$set": {PUBLISHED_FIELD: now}})
    return PublishOut(ice_breakers=bool(settings.ice_breakers), menu=bool(settings.menu), published_at=_iso(now) or "")


@router.get("/replies", response_model=list[OutboxItemOut])
async def tap_replies(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> list[OutboxItemOut]:
    """The newest replies our buttons, chips, ice breakers and menu have sent (or tried to)."""
    await _channel(db, channel_id)
    rows = await db.dm_outbox.find({"channel_id": channel_id}).sort("created_at", -1).limit(100).to_list(100)
    return [
        OutboxItemOut(
            key=str(r["key"]),
            recipient_id=str(r["recipient_id"]),
            reason=str(r.get("reason") or ""),
            status=str(r.get("status") or ""),
            error=r.get("error"),
            follows=r.get("follows"),
            gated=bool(r.get("gated")),
            text=str((r.get("message") or {}).get("text") or ""),
            created_at=_iso(r.get("created_at")),
            sent_at=_iso(r.get("sent_at")),
        )
        for r in rows
    ]


class CityDemandOut(BaseModel):
    # Accent-free, lower case ("istanbul"); the key the tally groups by.
    normalised: str
    name: str
    slug: str | None
    outcome: str
    count: int
    people: int
    last_at: str | None
    # Likes on the comments that named it (read by the comment poll; 0 until it has run): the sum, and
    # the most any one of them got, for "the most-liked city is our next video".
    likes: int = 0
    top_likes: int = 0


class CityAnswerOut(BaseModel):
    text: str
    name: str
    normalised: str
    slug: str | None
    outcome: str
    replied: bool
    reply_kind: str
    peer_id: str
    answered_at: str | None
    source: str = ""
    media_id: str = ""
    username: str = ""
    like_count: int | None = None


class CityAnswersOut(BaseModel):
    demand: list[CityDemandOut]
    recent: list[CityAnswerOut]


@router.get("/city-answers", response_model=CityAnswersOut)
async def city_answers(
    channel_id: str,
    media_id: str | None = Query(None, description="Only the comments on this reel (a city vote's reel)"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> CityAnswersOut:
    """Cities asked for in DMs and comments: a tally by city (most asked first), and the latest answers."""
    await _channel(db, channel_id)
    query: dict[str, Any] = {"channel_id": channel_id}
    if media_id:
        query["media_id"] = media_id
    rows = await db.dm_city_answers.find(query).to_list(length=None)
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("outcome") != "ignored":
            # By the city, not by how they wrote it: "Rome" and "rome italy" are one ask for Rome.
            key = r.get("slug") or meet_city.fold(str(r.get("name") or "")) or r.get("normalised") or ""
            groups.setdefault(str(key), []).append(r)
    demand = [
        CityDemandOut(
            normalised=str(g[-1].get("normalised") or ""),
            name=str(g[-1].get("name") or ""),
            slug=g[-1].get("slug"),
            outcome=str(g[-1].get("outcome") or ""),
            count=len(g),
            people=len({r.get("peer_id") for r in g}),
            last_at=_iso(
                max((r["answered_at"] for r in g if isinstance(r.get("answered_at"), datetime)), default=None)
            ),
            likes=sum(int(r.get("like_count") or 0) for r in g),
            top_likes=max((int(r.get("like_count") or 0) for r in g), default=0),
        )
        for g in groups.values()
    ]
    demand.sort(key=lambda d: (-d.people, -d.count, d.normalised))
    # One reel's vote is shown whole; the channel-wide list is the latest 100.
    recent = sorted(rows, key=lambda r: assume_utc(r["created_at"]), reverse=True)[: None if media_id else 100]
    return CityAnswersOut(
        demand=demand,
        recent=[
            CityAnswerOut(
                text=str(r.get("text") or ""),
                name=str(r.get("name") or ""),
                normalised=str(r.get("normalised") or ""),
                slug=r.get("slug"),
                outcome=str(r.get("outcome") or ""),
                replied=bool(r.get("replied")),
                reply_kind=str(r.get("reply_kind") or ""),
                peer_id=str(r.get("peer_id") or ""),
                answered_at=_iso(r.get("answered_at")),
                source=str(r.get("source") or ""),
                media_id=str(r.get("media_id") or ""),
                username=str(r.get("username") or ""),
                like_count=r.get("like_count"),
            )
            for r in recent
        ],
    )
