"""Story kit routes: one authenticated call that records stories and emails the owner, and the channel's
single public story page.

The page carries no login (it is opened on a phone from an email), so its only lock is a 43-character
token, one per channel, stored on the channel as ``story_page_token``. It never expires; instead the page
only ever lists, and only ever serves files for, reels from the last three days onward. The video and
cover are streamed through the server rather than linked to R2, so the page can read the file into a File
for the iPhone share sheet without any bucket CORS.

Links from before the single page (one page per scheduling run, kept in ``story_kits``) redirect to the
channel's page while they would still have been valid.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger
from app.services import story_kit as kit

logger = get_logger(__name__)

api_router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/story-kit", tags=["story-kit"], dependencies=[Depends(verify_api_key)]
)
page_router = APIRouter(tags=["story-kit-page"])

PUBLIC_ORIGIN = "https://automation-server.tryalgoviz.com"


class KitReel(BaseModel):
    video_id: str
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,80}$")


class KitRequest(BaseModel):
    reels: list[KitReel] = Field(min_length=1, max_length=20)
    send_email: bool = True


class DoneRequest(BaseModel):
    done: bool


def _r2() -> Any:
    import app.main as main_mod

    return main_mod.r2_service


def page_url(token: str) -> str:
    return f"{PUBLIC_ORIGIN}/s/{token}"


async def channel_page_token(db: Any, channel_id: str) -> str:
    """The channel's story page token, made on first use and kept from then on."""
    channel = await db.channels.find_one({"channel_id": channel_id}, {"story_page_token": 1})
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Channel {channel_id} not found")
    if channel.get("story_page_token"):
        return str(channel["story_page_token"])
    # Only set when still absent, so two first calls racing agree on one token.
    await db.channels.update_one(
        {"channel_id": channel_id, "story_page_token": {"$exists": False}},
        {"$set": {"story_page_token": kit.new_token()}},
    )
    channel = await db.channels.find_one({"channel_id": channel_id}, {"story_page_token": 1})
    return str(channel["story_page_token"])


async def channel_page_url(db: Any, channel_id: str) -> str:
    return page_url(await channel_page_token(db, channel_id))


# Wide enough to catch a trial that graduates near the end of a week-long check.
_PAGE_QUERY_WINDOW = timedelta(days=11)


async def _page_items(db: Any, channel_id: str) -> list[kit.KitItem]:
    now = datetime.now(timezone.utc)
    videos = await db.videos.find(
        {
            "channel_id": channel_id,
            "story_slug": {"$exists": True, "$nin": [None, ""]},
            "scheduled_at": {"$gte": now - _PAGE_QUERY_WINDOW},
        },
        {
            "video_id": 1,
            "story_slug": 1,
            "title": 1,
            "scheduled_at": 1,
            "status": 1,
            "thumbnail_r2_key": 1,
            "trial_graduation": 1,
            "trial_check_state": 1,
            "trial_check_checked_at": 1,
            "story_scheduled_at": 1,
        },
    ).to_list(length=200)
    return kit.page_items(videos, now)


@api_router.post("")
async def create_kit(channel_id: str, body: KitRequest, db: Any = Depends(get_db)) -> dict[str, Any]:
    """Record each reel's story slug. No email: the story email goes out when the reel goes live.

    Business Suite cannot schedule the story, so a prompt at queue time (often a day ahead) is one the
    owner cannot act on. The graduation and go-live crons build their emails from the saved slug.
    ``send_email`` is still accepted so older callers keep working, and is ignored.
    """
    trials: list[str] = []
    for r in body.reels:
        video = await db.videos.find_one({"channel_id": channel_id, "video_id": r.video_id})
        if not video or not video.get("scheduled_at"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{r.video_id} is not a scheduled video of {channel_id}")
        await db.videos.update_one({"_id": video["_id"]}, {"$set": {"story_slug": r.slug}})
        if video.get("trial_graduation"):
            trials.append(r.video_id)
    return {"emailed": False, "kit": await channel_page_url(db, channel_id), "trials_without_story": trials}


async def _channel_for(db: Any, token: str) -> str:
    channel = await db.channels.find_one({"story_page_token": token}, {"channel_id": 1})
    if not channel:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return str(channel["channel_id"])


async def _listed(db: Any, token: str, video_id: str) -> tuple[kit.KitItem, dict[str, Any]]:
    """A reel on the page right now, with its video doc. Anything else is a 404: the token only opens these."""
    channel_id = await _channel_for(db, token)
    for item in await _page_items(db, channel_id):
        if item.video_id == video_id:
            video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
            if video:
                return item, video
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


def _stream(key: str, media_type: str, filename: str | None = None) -> StreamingResponse:
    obj = _r2()._client.get_object(Bucket=_r2()._bucket, Key=key)
    headers = {"Cache-Control": "private, max-age=3600", "Content-Length": str(obj["ContentLength"])}
    if filename:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return StreamingResponse(obj["Body"].iter_chunks(chunk_size=256 * 1024), media_type=media_type, headers=headers)


_PAGE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


@page_router.get("/s/{token}", response_class=HTMLResponse, response_model=None)
async def kit_page(token: str, db: Any = Depends(get_db)) -> HTMLResponse | RedirectResponse:
    channel = await db.channels.find_one({"story_page_token": token}, {"channel_id": 1})
    if channel:
        items = await _page_items(db, channel["channel_id"])
        return HTMLResponse(kit.kit_page(token, items), headers=_PAGE_HEADERS)
    # A link from a per-run email sent before the single page: send it on while it would still have worked.
    legacy = await db.story_kits.find_one({"token": token})
    if legacy and not kit.is_expired(legacy["expires_at"], datetime.now(timezone.utc)):
        return RedirectResponse(await channel_page_url(db, legacy["channel_id"]), status_code=status.HTTP_303_SEE_OTHER)
    # One answer for a wrong token and an expired one, so a guess learns nothing.
    raise HTTPException(status.HTTP_404_NOT_FOUND, "This page has expired")


@page_router.get("/s/{token}/{video_id}/video")
async def kit_video(token: str, video_id: str, db: Any = Depends(get_db)) -> StreamingResponse:
    item, video = await _listed(db, token, video_id)
    if not video.get("r2_object_key"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return _stream(video["r2_object_key"], "video/mp4", f"{item.slug}.mp4")


@page_router.get("/s/{token}/{video_id}/cover")
async def kit_cover(token: str, video_id: str, db: Any = Depends(get_db)) -> StreamingResponse:
    _, video = await _listed(db, token, video_id)
    if not video.get("thumbnail_r2_key"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return _stream(video["thumbnail_r2_key"], "image/jpeg")


@page_router.post("/s/{token}/{video_id}/done")
async def kit_done(token: str, video_id: str, body: DoneRequest, db: Any = Depends(get_db)) -> dict[str, bool]:
    """Tick or untick "Scheduled" for a reel on the page."""
    _, video = await _listed(db, token, video_id)
    update = (
        {"$set": {"story_scheduled_at": datetime.now(timezone.utc)}}
        if body.done
        else {"$unset": {"story_scheduled_at": ""}}
    )
    await db.videos.update_one({"_id": video["_id"]}, update)
    return {"done": body.done}
