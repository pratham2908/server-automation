"""Instagram image / carousel / story posts.

Thin HTTP layer over ``PostService``; the contract is ``docs/instagram-posts.md``.
The fixed paths (``/publishing-limit``, ``/instagram-feed``) are declared before
``/{post_id}`` so they are never read as a post id.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
from app.models.post import (
    DeletedOut,
    FeedPage,
    HandoffOut,
    MarkPublishedRequest,
    MediaInsightsOut,
    PostCreate,
    PostListOut,
    PostOut,
    PostUpdate,
    PublishingLimitOut,
    ScheduleRequest,
    SlideCreate,
    SlideUploadOut,
)
from app.services.post_publisher import wake_post_publisher
from app.services.post_service import PostError, PostService

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/posts",
    tags=["posts"],
    dependencies=[Depends(verify_api_key)],
)


def get_post_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> PostService:
    from app.main import instagram_service_manager, r2_service

    return PostService(db=db, r2=r2_service, instagram_manager=instagram_service_manager, wake=wake_post_publisher)


@asynccontextmanager
async def _http_errors() -> AsyncIterator[None]:
    try:
        yield
    except PostError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail)


# ---- fixed paths first -------------------------------------------------------


@router.get("", response_model=PostListOut)
async def list_posts(
    channel_id: str,
    status_filter: str | None = Query(None, alias="status"),
    service: PostService = Depends(get_post_service),
) -> PostListOut:
    async with _http_errors():
        return await service.list_posts(channel_id, status_filter)


@router.post("", response_model=PostOut, status_code=status.HTTP_201_CREATED)
async def create_post(channel_id: str, body: PostCreate, service: PostService = Depends(get_post_service)) -> PostOut:
    async with _http_errors():
        return await service.create_post(channel_id, body)


@router.get("/publishing-limit", response_model=PublishingLimitOut)
async def publishing_limit(channel_id: str, service: PostService = Depends(get_post_service)) -> PublishingLimitOut:
    async with _http_errors():
        return await service.publishing_limit(channel_id)


@router.get("/instagram-feed", response_model=FeedPage)
async def instagram_feed(
    channel_id: str,
    limit: int = Query(24, ge=1, le=50),
    after: str | None = None,
    include_reels: bool = False,
    service: PostService = Depends(get_post_service),
) -> FeedPage:
    async with _http_errors():
        return await service.feed(channel_id, limit, after, include_reels)


@router.get("/instagram-feed/{media_id}/insights", response_model=MediaInsightsOut)
async def media_insights(
    channel_id: str, media_id: str, service: PostService = Depends(get_post_service)
) -> MediaInsightsOut:
    async with _http_errors():
        return await service.insights(channel_id, media_id)


# ---- one post ----------------------------------------------------------------


@router.get("/{post_id}", response_model=PostOut)
async def get_post(channel_id: str, post_id: str, service: PostService = Depends(get_post_service)) -> PostOut:
    async with _http_errors():
        return await service.get_post(channel_id, post_id)


@router.patch("/{post_id}", response_model=PostOut)
async def update_post(
    channel_id: str, post_id: str, body: PostUpdate, service: PostService = Depends(get_post_service)
) -> PostOut:
    async with _http_errors():
        return await service.update_post(channel_id, post_id, body)


@router.delete("/{post_id}", response_model=DeletedOut)
async def delete_post(channel_id: str, post_id: str, service: PostService = Depends(get_post_service)) -> DeletedOut:
    async with _http_errors():
        return await service.delete_post(channel_id, post_id)


@router.post("/{post_id}/slides", response_model=SlideUploadOut)
async def create_slide(
    channel_id: str, post_id: str, body: SlideCreate, service: PostService = Depends(get_post_service)
) -> SlideUploadOut:
    async with _http_errors():
        return await service.create_slide(channel_id, post_id, body)


@router.post("/{post_id}/slides/{slide_id}/complete", response_model=PostOut)
async def complete_slide(
    channel_id: str, post_id: str, slide_id: str, service: PostService = Depends(get_post_service)
) -> PostOut:
    async with _http_errors():
        return await service.complete_slide(channel_id, post_id, slide_id)


@router.delete("/{post_id}/slides/{slide_id}", response_model=PostOut)
async def delete_slide(
    channel_id: str, post_id: str, slide_id: str, service: PostService = Depends(get_post_service)
) -> PostOut:
    async with _http_errors():
        return await service.delete_slide(channel_id, post_id, slide_id)


@router.post("/{post_id}/schedule", response_model=PostOut)
async def schedule_post(
    channel_id: str, post_id: str, body: ScheduleRequest, service: PostService = Depends(get_post_service)
) -> PostOut:
    async with _http_errors():
        return await service.schedule(channel_id, post_id, body.scheduled_at)


@router.post("/{post_id}/unschedule", response_model=PostOut)
async def unschedule_post(channel_id: str, post_id: str, service: PostService = Depends(get_post_service)) -> PostOut:
    async with _http_errors():
        return await service.unschedule(channel_id, post_id)


@router.post("/{post_id}/publish-now", response_model=PostOut)
async def publish_now(channel_id: str, post_id: str, service: PostService = Depends(get_post_service)) -> PostOut:
    async with _http_errors():
        return await service.publish_now(channel_id, post_id)


@router.post("/{post_id}/retry", response_model=PostOut)
async def retry_post(channel_id: str, post_id: str, service: PostService = Depends(get_post_service)) -> PostOut:
    async with _http_errors():
        return await service.retry(channel_id, post_id)


@router.post("/{post_id}/mark-published", response_model=PostOut)
async def mark_published(
    channel_id: str,
    post_id: str,
    body: MarkPublishedRequest | None = None,
    service: PostService = Depends(get_post_service),
) -> PostOut:
    async with _http_errors():
        return await service.mark_published(channel_id, post_id, body or MarkPublishedRequest())


@router.post("/{post_id}/archive", response_model=PostOut)
async def archive_post(channel_id: str, post_id: str, service: PostService = Depends(get_post_service)) -> PostOut:
    async with _http_errors():
        return await service.archive(channel_id, post_id)


@router.post("/{post_id}/restore", response_model=PostOut)
async def restore_post(channel_id: str, post_id: str, service: PostService = Depends(get_post_service)) -> PostOut:
    async with _http_errors():
        return await service.restore(channel_id, post_id)


@router.get("/{post_id}/handoff", response_model=HandoffOut)
async def post_handoff(channel_id: str, post_id: str, service: PostService = Depends(get_post_service)) -> HandoffOut:
    async with _http_errors():
        return await service.handoff(channel_id, post_id)
