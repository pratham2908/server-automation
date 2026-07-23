"""External router — scoped access for creator applications.

Every route is protected by ``verify_channel_api_key``, which validates the
``X-Channel-Api-Key`` header and confirms the key belongs to the channel_id
in the path.  A valid key for channel A is rejected on channel B's endpoints.

Scope: capabilities discovery, upload, list/status, metadata update, schedule,
publish.  Never exposes OAuth tokens, R2 keys, or internal analysis data.
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
# GET / — capabilities discovery
# ──────────────────────────────────────────────────────────────────────────────

_CAPABILITIES: dict[str, Any] = {
    "api_version": "1.0",
    "auth": {
        "header": "X-Channel-Api-Key",
        "description": (
            "Pass your channel API key in this header on every request. "
            "Keys are scoped per-channel — a key for channel A is rejected on channel B."
        ),
    },
    "video_statuses": {
        "ready": "File stored; AI analysis may still be running (check packaging_status).",
        "queued": "Scheduled for publishing; awaiting the publish window.",
        "published": "Live on the platform.",
        "failed": "Publishing failed.",
        "scheduled": "Queued for a specific publish time (Instagram only).",
    },
    "packaging_status": {
        "analyzing": "AI analysis (retention scoring, title/description/tag suggestions) is in progress.",
        "completed": "AI analysis finished. The video is ready to schedule or publish.",
        "failed": "AI analysis failed. The video can still be published with its original metadata.",
        "(absent)": "Analysis has not started yet — poll again in a moment.",
    },
    "analysis_polling": (
        "After upload, poll GET /videos/{video_id} every 5-10 seconds until "
        "packaging_status == 'completed'. That signals AI analysis is fully done."
    ),
    "endpoints": [
        {
            "name": "capabilities",
            "method": "GET",
            "path": "/api/v1/ext/{channel_id}/",
            "description": "Returns this document. Call any time to discover the current API contract.",
            "auth_required": True,
            "request": {"headers": {"X-Channel-Api-Key": "string"}},
            "response": {"api_version": "string", "endpoints": "array"},
        },
        {
            "name": "upload_video",
            "method": "POST",
            "path": "/api/v1/ext/{channel_id}/upload",
            "description": (
                "Upload a video file. The file is stored immediately and AI analysis "
                "(retention scoring, auto-packaging, metadata suggestions) is queued "
                "in the background. Returns a video_id to use in all subsequent calls."
            ),
            "auth_required": True,
            "request": {
                "content_type": "multipart/form-data",
                "fields": {
                    "file": {"type": "file", "required": True, "description": "Video file (mp4 recommended)"},
                    "title": {"type": "string", "required": False, "description": "Falls back to filename if omitted"},
                    "description": {"type": "string", "required": False},
                    "tags": {"type": "string", "required": False, "description": "Comma-separated tags"},
                    "scheduled_at": {
                        "type": "string",
                        "required": False,
                        "description": "UTC ISO 8601 datetime to auto-schedule on upload, e.g. 2026-08-01T09:00:00Z",
                    },
                },
            },
            "response": {
                "ok": "boolean",
                "video_id": "string — use this in all subsequent calls",
                "status": "string — initial status, usually 'uploading' or 'analyzing'",
                "analysis_queued": "boolean",
            },
        },
        {
            "name": "list_videos",
            "method": "GET",
            "path": "/api/v1/ext/{channel_id}/videos",
            "description": (
                "List all videos for this channel. Use ?status= to poll for "
                "analysis completion (status == 'ready') before scheduling."
            ),
            "auth_required": True,
            "request": {
                "query_params": {
                    "status": {
                        "type": "string",
                        "required": False,
                        "description": "Filter by status: uploading | analyzing | ready | queued | published | failed",
                    }
                }
            },
            "response": {
                "videos": "array of video objects (see video_fields below)",
            },
            "video_fields": {
                "video_id": "string",
                "title": "string",
                "description": "string",
                "tags": "array of strings",
                "status": "string",
                "packaging_status": "string — whether AI packaging has run",
                "scheduled_at": "ISO datetime or null",
                "published_at": "ISO datetime or null",
                "created_at": "ISO datetime",
            },
        },
        {
            "name": "update_metadata",
            "method": "PATCH",
            "path": "/api/v1/ext/{channel_id}/videos/{video_id}/metadata",
            "description": (
                "Update title, description, or tags. All fields are optional — send "
                "only what you want to change. Tags is a full replacement, not an append."
            ),
            "auth_required": True,
            "request": {
                "content_type": "application/json",
                "body": {
                    "title": {"type": "string", "required": False},
                    "description": {"type": "string", "required": False},
                    "tags": {"type": "array of strings", "required": False, "note": "Full replacement"},
                },
            },
            "response": {"ok": "boolean"},
        },
        {
            "name": "schedule_video",
            "method": "POST",
            "path": "/api/v1/ext/{channel_id}/videos/{video_id}/schedule",
            "description": (
                "Set a future UTC publish time. Video must be in 'ready' or 'queued' status. "
                "If already queued, this reschedules it to the new time."
            ),
            "auth_required": True,
            "request": {
                "content_type": "application/json",
                "body": {
                    "scheduled_at": {
                        "type": "string",
                        "required": True,
                        "description": "Future UTC ISO 8601 datetime, e.g. 2026-08-01T09:00:00Z",
                    }
                },
            },
            "response": {"ok": "boolean", "scheduled_at": "ISO datetime — the confirmed publish time"},
        },
        {
            "name": "publish_now",
            "method": "POST",
            "path": "/api/v1/ext/{channel_id}/videos/{video_id}/publish",
            "description": (
                "Queue the video for immediate publishing. The background publisher picks it up "
                "within minutes. Video must be in 'ready' status. "
                "Poll GET /videos/{video_id} for final status and platform_id."
            ),
            "auth_required": True,
            "request": {"body": None},
            "response": {
                "ok": "boolean",
                "queued": "boolean",
                "message": "string — human-readable confirmation",
            },
        },
        {
            "name": "get_video",
            "method": "GET",
            "path": "/api/v1/ext/{channel_id}/videos/{video_id}",
            "description": (
                "Fetch a single video by its video_id. Use this to poll packaging_status "
                "after upload without fetching the full video list."
            ),
            "auth_required": True,
            "request": {},
            "response": "video object (same fields as list_videos entries)",
        },
        {
            "name": "sync",
            "method": "POST",
            "path": "/api/v1/ext/{channel_id}/sync",
            "description": (
                "Sync this channel's video library with the platform (YouTube or Instagram). "
                "Fetches all videos from the platform API: updates metadata, views, likes, and "
                "comments on existing records, and imports any videos that exist on the platform "
                "but are not yet in the system. Returns a summary of what changed."
            ),
            "auth_required": True,
            "request": {"body": None},
            "response": {
                "ok": "boolean",
                "synced": "integer — number of new videos imported from the platform",
            },
        },
    ],
    "error_codes": {
        "401": "Missing or invalid X-Channel-Api-Key header",
        "404": "Video not found, or does not belong to this channel",
        "422": "Validation error — check error detail for the specific field",
    },
    "notes": [
        "All datetimes are UTC ISO 8601.",
        "OAuth tokens, R2 storage keys, and internal AI analysis data are never returned.",
        "Publish is asynchronous — poll GET /videos for final status and platform_id.",
    ],
}


@router.get("/", summary="API capabilities discovery")
async def get_capabilities(channel_id: str) -> dict[str, Any]:
    """Returns the full API contract for this channel's external integration.

    Call this endpoint to discover available operations, request/response shapes,
    video status definitions, and error codes.  The response is versioned — check
    ``api_version`` on each poll to detect breaking changes automatically.
    """
    return {"channel_id": channel_id, **_CAPABILITIES}


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
        "message": "Queued for immediate publishing. Poll GET /videos/{video_id} to confirm status == 'published' and retrieve the platform_id.",
    }


# ──────────────────────────────────────────────────────────────────────────────
# GET /videos/{video_id}
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/videos/{video_id}")
async def get_video(
    channel_id: str,
    video_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """Fetch a single video by its video_id.

    Use this to efficiently poll ``packaging_status`` after upload without
    fetching the full video list. Poll every 5–10 seconds until
    ``packaging_status == "completed"`` — that signals AI analysis is done
    and the video is ready to schedule or publish.
    """
    video = await db.videos.find_one(
        {"channel_id": channel_id, "video_id": video_id},
        {"retention": 0, "performance": 0},
    )
    if not video:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Video '{video_id}' not found")
    return _public_video(video)


# ──────────────────────────────────────────────────────────────────────────────
# POST /sync
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/sync")
async def sync_channel(
    channel_id: str,
    service: VideoService = Depends(_get_service),
) -> dict[str, Any]:
    """Sync this channel's video library with the platform.

    Identical to the "Sync Videos" button in the dashboard: fetches all videos
    from YouTube or Instagram, updates metadata (views, likes, comments,
    thumbnail, status) on existing records, and imports any videos that exist
    on the platform but are not yet in the system.

    Returns a summary with the count of newly imported videos.
    """
    try:
        return await service.sync_videos(channel_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))
