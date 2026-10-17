"""Story kit routes: one authenticated call that makes and emails a kit, and the public token-locked page.

The page carries no login (it is opened on a phone from an email), so its only lock is a 43-character
token that expires after 3 days. The video and cover are streamed through the server rather than linked
to R2, so the page can read the file into a File for the iPhone share sheet without any bucket CORS.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger
from app.services import story_kit as kit
from app.services.email_service import resolve_owner_email, send_email

logger = get_logger(__name__)

api_router = APIRouter(prefix="/api/v1/channels/{channel_id}/story-kit", tags=["story-kit"], dependencies=[Depends(verify_api_key)])
page_router = APIRouter(tags=["story-kit-page"])

PUBLIC_ORIGIN = "https://automation-server.tryalgoviz.com"


class KitReel(BaseModel):
    video_id: str
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,80}$")


class KitRequest(BaseModel):
    reels: list[KitReel] = Field(min_length=1, max_length=20)
    send_email: bool = True


def _r2() -> Any:
    import app.main as main_mod

    return main_mod.r2_service


def page_url(token: str) -> str:
    return f"{PUBLIC_ORIGIN}/s/{token}"


async def make_kit(db: Any, channel_id: str, reels: list[tuple[dict[str, Any], str]], now: datetime) -> dict[str, Any]:
    """Store one kit for these (video doc, slug) pairs and return it with its token."""
    items = [
        {
            "video_id": v["video_id"],
            "slug": slug,
            "title": v.get("title") or slug,
            "reel_time": v["scheduled_at"],
            "video_key": v.get("r2_object_key"),
            "thumb_key": v.get("thumbnail_r2_key"),
        }
        for v, slug in reels
    ]
    doc = {
        "token": kit.new_token(),
        "channel_id": channel_id,
        "items": items,
        "created_at": now,
        "expires_at": now + kit.KIT_LIFETIME,
    }
    await db.story_kits.insert_one(dict(doc))
    return doc


@api_router.post("")
async def create_kit(channel_id: str, body: KitRequest, db: Any = Depends(get_db)) -> dict[str, Any]:
    """Record each reel's story slug, then email a kit for the normal posts.

    A trial is meant for non-followers, so it gets no story when posted. Its slug is still saved on the
    video, and the graduation email builds its story card from that if the trial clears its bar.
    """
    now = datetime.now(timezone.utc)
    normal: list[tuple[dict[str, Any], str]] = []
    trials: list[str] = []
    for r in body.reels:
        video = await db.videos.find_one({"channel_id": channel_id, "video_id": r.video_id})
        if not video or not video.get("scheduled_at"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{r.video_id} is not a scheduled video of {channel_id}")
        await db.videos.update_one({"_id": video["_id"]}, {"$set": {"story_slug": r.slug}})
        if video.get("trial_graduation"):
            trials.append(r.video_id)
        else:
            normal.append((video, r.slug))
    if not normal:
        return {"emailed": False, "kit": None, "trials_without_story": trials}
    doc = await make_kit(db, channel_id, normal, now)
    r2 = _r2()
    items = []
    for it in doc["items"]:
        thumb = r2.generate_presigned_url(it["thumb_key"], expires_in=int(kit.KIT_LIFETIME.total_seconds())) if it.get("thumb_key") else None
        items.append(kit.KitItem(it["video_id"], it["slug"], it["title"], kit.reel_time_ist(it["reel_time"]), thumb))
    url = page_url(doc["token"])
    subject, text, html = kit.kit_email(items, url)
    sent = False
    if body.send_email:
        settings = get_settings()
        sent = await send_email(settings, await resolve_owner_email(db, settings), subject, text, html)
    return {"emailed": sent, "kit": url, "stories": [{"video_id": i.video_id, "story_at": i.story_time.isoformat()} for i in items],
            "trials_without_story": trials}


async def _live_kit(db: Any, token: str) -> dict[str, Any]:
    doc = await db.story_kits.find_one({"token": token})
    # One answer for a wrong token and an expired one, so a guess learns nothing.
    if not doc or kit.is_expired(doc["expires_at"], datetime.now(timezone.utc)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This page has expired")
    return doc


def _item(doc: dict[str, Any], video_id: str) -> dict[str, Any]:
    for it in doc["items"]:
        if it["video_id"] == video_id:
            return it
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


def _stream(key: str, media_type: str, filename: str | None = None) -> StreamingResponse:
    obj = _r2()._client.get_object(Bucket=_r2()._bucket, Key=key)
    headers = {"Cache-Control": "private, max-age=3600", "Content-Length": str(obj["ContentLength"])}
    if filename:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return StreamingResponse(obj["Body"].iter_chunks(chunk_size=256 * 1024), media_type=media_type, headers=headers)


@page_router.get("/s/{token}", response_class=HTMLResponse)
async def kit_page(token: str, db: Any = Depends(get_db)) -> HTMLResponse:
    doc = await _live_kit(db, token)
    return HTMLResponse(kit.kit_page(token, kit.items_from_doc(doc)), headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@page_router.get("/s/{token}/{video_id}/video")
async def kit_video(token: str, video_id: str, db: Any = Depends(get_db)) -> StreamingResponse:
    item = _item(await _live_kit(db, token), video_id)
    if not item.get("video_key"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return _stream(item["video_key"], "video/mp4", f"{item['slug']}.mp4")


@page_router.get("/s/{token}/{video_id}/cover")
async def kit_cover(token: str, video_id: str, db: Any = Depends(get_db)) -> StreamingResponse:
    item = _item(await _live_kit(db, token), video_id)
    if not item.get("thumb_key"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return _stream(item["thumb_key"], "image/jpeg")
