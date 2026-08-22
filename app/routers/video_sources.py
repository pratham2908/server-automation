"""Video sources router — read a channel app's export API and import from it.

Registering a source accepts credentials, so that one route requires a signed-in
profile on top of the API key the rest of the router uses, and nothing ever sends
a credential back: every response is a :class:`VideoSourcePublic`, which has no
field capable of carrying one. ``scripts/seed_video_source.py`` remains the way to
seed a source without a browser.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import get_current_profile, verify_api_key
from app.models.profile import ProfileInDB
from app.models.video_source import (
    ImportRequest,
    SourceCreateRequest,
    SourceListResponse,
    VideoSourcePublic,
)
from app.services.video_source_service import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, VideoSourceService
from app.services.video_sources.schema import SourceKindInfo, describe_kinds

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/video-sources",
    tags=["video-sources"],
    dependencies=[Depends(verify_api_key)],
)

# Separate prefix so job listing can never be shadowed by the `{source_id}` routes.
imports_router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/source-imports",
    tags=["video-sources"],
    dependencies=[Depends(verify_api_key)],
)


# No channel in the path: the kinds the server can talk to are the same everywhere.
kinds_router = APIRouter(
    prefix="/api/v1/video-source-kinds",
    tags=["video-sources"],
    dependencies=[Depends(verify_api_key)],
)


def get_source_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> VideoSourceService:
    return VideoSourceService(db)


@kinds_router.get("/", response_model=list[SourceKindInfo])
async def list_source_kinds() -> list[SourceKindInfo]:
    """The content app kinds this server supports, and the form each one needs.

    Derived from the config models themselves, so a newly added kind appears here
    — and in the UI — without a frontend change.
    """
    return describe_kinds()


@router.get("/", response_model=list[VideoSourcePublic])
async def list_sources(
    channel_id: str,
    service: VideoSourceService = Depends(get_source_service),
) -> list[VideoSourcePublic]:
    """List the channel's registered content apps (secrets redacted)."""
    return await service.list_sources(channel_id)


@router.post("/", response_model=VideoSourcePublic, status_code=status.HTTP_201_CREATED)
async def create_source(
    channel_id: str,
    payload: SourceCreateRequest,
    service: VideoSourceService = Depends(get_source_service),
    current_profile: ProfileInDB = Depends(get_current_profile),
) -> VideoSourcePublic:
    """Register a content app for this channel.

    Accepts credentials, hence the signed-in profile. The app is called before
    anything is stored, so a source that cannot answer is never created.
    """
    try:
        return await service.create_source(channel_id, payload)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ConnectionError as exc:
        # The credentials or the URL are wrong, or the app is down. Either way we
        # stored nothing, so the form is still the place to fix it.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/{source_id}/test")
async def test_source(
    channel_id: str,
    source_id: str,
    service: VideoSourceService = Depends(get_source_service),
) -> dict[str, Any]:
    """Verify the app is reachable and our credentials work; records health."""
    try:
        return await service.test_connection(channel_id, source_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@router.get("/{source_id}/videos", response_model=SourceListResponse)
async def list_source_videos(
    channel_id: str,
    source_id: str,
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    cursor: str | None = Query(None, description="nextCursor from a previous page"),
    service: VideoSourceService = Depends(get_source_service),
) -> SourceListResponse:
    """A page of the app's finished videos, flagging ones already imported."""
    try:
        return await service.list_videos(channel_id, source_id, limit, cursor)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    except ConnectionError as exc:
        # The app is unreachable or rejected us — that is upstream, not our caller's
        # fault, so 502 rather than 400.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/{source_id}/videos/{video_id}/mark-sent")
async def mark_video_sent(
    channel_id: str,
    source_id: str,
    video_id: str,
    service: VideoSourceService = Depends(get_source_service),
) -> dict[str, Any]:
    """Tell the app it has already delivered this video, so it stops offering it.

    For retiring something the channel published through another route, which the
    app has no way to know about and would otherwise keep listing as unsent.
    """
    try:
        return await service.mark_sent(channel_id, source_id, video_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@router.post("/{source_id}/import")
async def import_videos(
    channel_id: str,
    source_id: str,
    payload: ImportRequest,
    service: VideoSourceService = Depends(get_source_service),
) -> dict[str, Any]:
    """Queue one or more of the app's videos for server-side transfer into our R2."""
    try:
        return await service.enqueue_import(
            channel_id,
            source_id,
            payload.video_ids,
            scheduled_at=payload.scheduled_at,
            analyze=payload.analyze,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    except ConnectionError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@imports_router.get("/")
async def list_import_jobs(
    channel_id: str,
    limit: int = Query(50, ge=1, le=200),
    service: VideoSourceService = Depends(get_source_service),
) -> dict[str, Any]:
    """Recent import jobs for this channel, newest first."""
    jobs = await service.list_imports(channel_id, limit)
    active = sum(1 for j in jobs if j["status"] in ("queued", "transferring"))
    return {"jobs": jobs, "active": active}
