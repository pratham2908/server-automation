"""Video sources router — read a channel app's export API and import from it.

Credentials are seeded directly into the ``video_sources`` collection (see
``scripts/seed_video_source.py``); this router deliberately exposes no create or
update route, so shared secrets never travel through the API.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
from app.models.video_source import ImportRequest, SourceListResponse, VideoSourcePublic
from app.services.video_source_service import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, VideoSourceService

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


def get_source_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> VideoSourceService:
    return VideoSourceService(db)


@router.get("/", response_model=list[VideoSourcePublic])
async def list_sources(
    channel_id: str,
    service: VideoSourceService = Depends(get_source_service),
) -> list[VideoSourcePublic]:
    """List the channel's registered content apps (secrets redacted)."""
    return await service.list_sources(channel_id)


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
    except ConnectionError as exc:
        # The app is unreachable or rejected us — that is upstream, not our caller's
        # fault, so 502 rather than 400.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


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
