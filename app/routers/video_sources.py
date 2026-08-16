"""Video sources router — browse a channel's external bucket and import from it.

Credentials are seeded directly into the ``video_sources`` collection (see
``scripts/seed_video_source.py``); this router deliberately exposes no create or
update route, so secrets never travel through the API.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
from app.models.video_source import ImportRequest, SourceBrowseResponse, VideoSourcePublic
from app.services.video_source_service import VideoSourceService

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
    """List the channel's registered video sources (secrets redacted)."""
    return await service.list_sources(channel_id)


@router.post("/{source_id}/test")
async def test_source(
    channel_id: str,
    source_id: str,
    service: VideoSourceService = Depends(get_source_service),
) -> dict[str, Any]:
    """Verify the source's credentials and record the result as health state."""
    try:
        return await service.test_connection(channel_id, source_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{source_id}/objects", response_model=SourceBrowseResponse)
async def browse_source(
    channel_id: str,
    source_id: str,
    prefix: str | None = Query(None, description="Folder to list; defaults to the source root"),
    cursor: str | None = Query(None, description="Continuation token from a previous page"),
    service: VideoSourceService = Depends(get_source_service),
) -> SourceBrowseResponse:
    """List video objects in the source bucket, flagging ones already imported."""
    try:
        return await service.browse(channel_id, source_id, prefix, cursor)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{source_id}/import")
async def import_objects(
    channel_id: str,
    source_id: str,
    payload: ImportRequest,
    service: VideoSourceService = Depends(get_source_service),
) -> dict[str, Any]:
    """Queue one or more source objects for server-side transfer into our R2."""
    try:
        return await service.enqueue_import(
            channel_id,
            source_id,
            payload.keys,
            scheduled_at=payload.scheduled_at,
            analyze=payload.analyze,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


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
