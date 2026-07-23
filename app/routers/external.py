"""External router — scoped access for creator applications.

Every route is protected by ``verify_channel_api_key``, which validates the
``X-Channel-Api-Key`` header and confirms the key belongs to the channel_id
in the path.  A valid key for channel A is rejected on channel B's endpoints.

Scope: upload, list/status, metadata update, schedule, publish.
Never exposes OAuth tokens, R2 keys, or internal analysis data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import verify_channel_api_key
from app.logger import get_logger
from app.services.video_service import VideoService

logger = get_logger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Router
# ──────────────────────────────────────────────────────────────────────────────

router = APIRouter(
    prefix="/api/v1/ext/{channel_id}",
    tags=["external"],
    dependencies=[Depends(verify_channel_api_key)],
)

# ──────────────────────────────────────────────────────────────────────────────
# Shared dependency — VideoService with all platform services injected
# ──────────────────────────────────────────────────────────────────────────────


def _get_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> VideoService:
    from app.main import gemini_service, instagram_service_manager, r2_service, youtube_service_manager

    return VideoService(
        db=db,
        r2_service=r2_service,
        gemini_service=gemini_service,
        youtube_manager=youtube_service_manager,
        instagram_manager=instagram_service_manager,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────────────

_EXTERNAL_VIDEO_FIELDS = {
    "video_id", "title", "description", "tags",
    "status", "packaging_status",
    "scheduled_at", "published_at", "created_at",
}

_STRIP_FIELDS = {
    "r2_object_key", "content_params", "ai_packaging", "retention",
    "performance", "multi_channel_group_id", "verification_status",
    "suggested", "channel_id", "_id",
}


def _public_video(v: dict[str, Any]) -> dict[str, Any]:
    """Return only the fields a creator app should see."""
    out: dict[str, Any] = {}
    for field in _EXTERNAL_VIDEO_FIELDS:
        val = v.get(field)
        # Serialise datetime objects to ISO strings
        if isinstance(val, datetime):
            val = val.isoformat()
        out[field] = val
    return out


class ScheduleBody(BaseModel):
    scheduled_at: str


class MetadataBody(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None


# ──────────────────────────────────────────────────────────────────────────────
# POST /upload
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/upload", status_code=status.HTTP_200_OK)
async def upload_video(
    channel_id: str,
    file: UploadFile = File(...),
    title: str = Form(""),
    description: str = Form(""),
    tags: str = Form(""),
    scheduled_at: str | None = Form(None),
    service: VideoService = Depends(_get_service),
) -> dict[str, Any]:
    """Upload a video file to the channel.

    The file is stored in R2 and AI analysis (retention scoring, packaging,
    metadata suggestions) is queued automatically.  The ``video_id`` in the
    response is the handle for all subsequent operations.
    """
    effective_title = title.strip() or (file.filename or "Untitled")
    try:
        result = await service.create_video(
            channel_id,
            file.file,
            effective_title,
            description,
            tags,
            None,   # category — creator apps don't manage categories
            None,   # content_params
            scheduled_at,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    video = result.get("video", {})
    return {
        "ok": True,
        "video_id": video.get("video_id"),
        "status": video.get("status"),
        "analysis_queued": True,
    }


# ──────────────────────────────────────────────────────────────────────────────
# GET /videos
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/videos")
async def list_videos(
    channel_id: str,
    status: str | None = Query(None, description="Filter: ready, scheduled, published, uploading"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """List videos for this channel.

    OAuth tokens and all internal fields are stripped.  Use ``?status=`` to
    narrow results, e.g. ``?status=ready`` while polling for analysis completion.
    """
    query: dict[str, Any] = {"channel_id": channel_id}
    if status:
        query["status"] = status

    docs = await db.videos.find(query, {"retention": 0, "performance": 0}).to_list(length=None)
    return {"videos": [_public_video(v) for v in docs]}


# ──────────────────────────────────────────────────────────────────────────────
# PATCH /videos/{video_id}/metadata
# ──────────────────────────────────────────────────────────────────────────────


@router.patch("/videos/{video_id}/metadata")
async def update_metadata(
    channel_id: str,
    video_id: str,
    body: MetadataBody,
    service: VideoService = Depends(_get_service),
) -> dict[str, Any]:
    """Update title, description, or tags.

    All fields are optional — send only what you want to change.
    The server verifies the video belongs to this channel before writing.
    ``tags`` is a full replacement, not an append.
    """
    updates: dict[str, Any] = {}
    if body.title is not None:
        updates["title"] = body.title
    if body.description is not None:
        updates["description"] = body.description
    if body.tags is not None:
        updates["tags"] = body.tags

    if not updates:
        return {"ok": True}

    try:
        return await service.update_video_metadata(channel_id, video_id, updates)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


# ──────────────────────────────────────────────────────────────────────────────
# POST /videos/{video_id}/schedule
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/videos/{video_id}/schedule")
async def schedule_video(
    channel_id: str,
    video_id: str,
    body: ScheduleBody,
    service: VideoService = Depends(_get_service),
) -> dict[str, Any]:
    """Set a UTC publish time for the video.

    ``scheduled_at`` must be a future UTC ISO 8601 datetime, e.g.
    ``2026-07-25T09:00:00Z``.  Past datetimes are rejected.

    If the video is already queued, the time is updated via reschedule.
    """
    try:
        parsed = datetime.fromisoformat(body.scheduled_at.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="scheduled_at must be a valid ISO 8601 datetime, e.g. 2026-07-25T09:00:00Z",
        )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    if parsed <= datetime.now(timezone.utc):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="scheduled_at must be in the future",
        )

    # Fetch current status to decide which service method to call
    db = service.db
    video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if not video:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Video '{video_id}' not found")

    current_status = video.get("status")

    try:
        if current_status == "queued":
            await service.reschedule_video(channel_id, video_id, parsed)
        elif current_status == "ready":
            await service.schedule_video(channel_id, video_id, parsed)
        else:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Video status is '{current_status}' — only 'ready' or 'queued' videos can be scheduled",
            )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return {"ok": True, "scheduled_at": parsed.isoformat()}


# ──────────────────────────────────────────────────────────────────────────────
# POST /videos/{video_id}/publish
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/videos/{video_id}/publish")
async def publish_video(
    channel_id: str,
    video_id: str,
    service: VideoService = Depends(_get_service),
) -> dict[str, Any]:
    """Queue the video for immediate publishing.

    Schedules the video with ``scheduled_at = now``, which the background
    publisher picks up on its next cycle (typically within a few minutes).
    The platform ID (YouTube video ID or Instagram media ID) becomes available
    in the video status once publishing completes — poll ``GET /videos`` for it.

    The video must be in ``ready`` status.  Use ``GET /videos`` to confirm
    ``status == "published"`` after publishing.
    """
    db = service.db
    video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if not video:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Video '{video_id}' not found")

    current_status = video.get("status")
    if current_status != "ready":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Video status is '{current_status}' — only 'ready' videos can be published",
        )

    from app.timezone import now_ist

    scheduled_at = now_ist()

    try:
        await service.schedule_video(channel_id, video_id, scheduled_at)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return {
        "ok": True,
        "queued": True,
        "message": "Queued for immediate publishing. Poll GET /videos to confirm status == 'published' and retrieve the platform_id.",
    }
