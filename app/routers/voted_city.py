"""Voted-city reels: the live vote, the owner's posting kits, and their follow-up.

See ``app.services.voted_city`` for the rules and ``app.services.voted_city_store`` for how it runs.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.config import get_settings
from app.database import get_db
from app.dependencies import verify_api_key
from app.services import voted_city_store as store
from app.services.voted_city_worker import graph_for
from app.timezone import now_ist

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/city-vote",
    tags=["voted-city"],
    dependencies=[Depends(verify_api_key)],
)

MAX_VIDEO_BYTES = 500 * 1024 * 1024
CHUNK = 1024 * 1024
VIDEO_TYPES = {"video/mp4", "video/quicktime", "application/octet-stream"}


class DoneIn(BaseModel):
    city: str = Field(min_length=1, max_length=80)
    media_id: str | None = None
    note: str | None = Field(default=None, max_length=500)
    comment_ids: list[str] = Field(default_factory=list, max_length=20)


class LinkIn(BaseModel):
    media_id: str = Field(min_length=1, max_length=40)


def _deps() -> tuple[Any, Any]:
    import app.main as main_mod

    return main_mod.instagram_service_manager, main_mod.r2_service


@asynccontextmanager
async def _errors() -> AsyncIterator[None]:
    try:
        yield
    except store.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except store.VoteError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


async def _graph(db: AsyncIOMotorDatabase, channel_id: str) -> tuple[Any, str, str]:
    manager, _ = _deps()
    conn = await graph_for(db, manager, channel_id)
    if conn is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="No Instagram connection for this channel")
    return conn


@router.get("")
async def city_vote(
    channel_id: str,
    live: bool = Query(True, description="Read every comment's likes from Instagram now (else the last kept)"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """The vote: cities by total likes on the comments asking for them, done cities left out."""
    graph = (await _graph(db, channel_id))[0] if live else None
    async with _errors():
        try:
            return await store.tally(db, graph, channel_id, now_ist(), live=live)
        except (store.NotFoundError, store.VoteError):
            raise
        except Exception as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Couldn't read live likes from Instagram: {exc}"
            ) from exc


@router.get("/recent")
async def recent_voted_posts(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> list[dict[str, Any]]:
    """Dry run of the detector: each "You voted." post among the newest 25 and what it would do with it."""
    graph, ig_user_id, _ = await _graph(db, channel_id)
    return await store.detect(db, graph, channel_id, ig_user_id, now_ist(), dry_run=True)


@router.get("/done")
async def list_done(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> list[dict[str, Any]]:
    return await store.done_cities(db, channel_id)


@router.post("/done", status_code=status.HTTP_201_CREATED)
async def mark_done(channel_id: str, body: DoneIn, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    """Leave a city out of the vote: posted without a kit, or not to be voted for again."""
    async with _errors():
        await store._channel(db, channel_id)
        return await store.mark_done(
            db, channel_id, body.city, now_ist(), media_id=body.media_id, note=body.note, comment_ids=body.comment_ids
        )


@router.delete("/done/{city}")
async def unmark_done(channel_id: str, city: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, bool]:
    if not await store.unmark_done(db, channel_id, city):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="That city isn't marked done")
    return {"removed": True}


@router.get("/kits")
async def list_kits(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> list[dict[str, Any]]:
    _, r2 = _deps()
    return [store.kit_out(k, r2) for k in await store.list_kits(db, channel_id)]


@router.post("/kits", status_code=status.HTTP_201_CREATED)
async def create_kit(
    channel_id: str,
    city: str = Form(..., min_length=1, max_length=80),
    votes: int = Form(..., ge=1, description="The like count the reel shows; at most the live total"),
    caption_body: str | None = Form(None, max_length=2000),
    file: UploadFile = File(...),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """Make the owner's posting kit for the city's voted reel: checks the vote live, keeps the mp4, emails."""
    if (file.content_type or "").lower() not in VIDEO_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Send the reel as an mp4")
    graph = (await _graph(db, channel_id))[0]
    _, r2 = _deps()
    with tempfile.SpooledTemporaryFile(max_size=32 * CHUNK) as video:
        size = 0
        while chunk := await file.read(CHUNK):
            size += len(chunk)
            if size > MAX_VIDEO_BYTES:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="The reel can be at most 500 MB")
            video.write(chunk)
        if not size:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="The video is empty")
        video.seek(0)
        async with _errors():
            kit = await store.create_kit(
                db,
                graph,
                r2,
                get_settings(),
                channel_id,
                city=city,
                votes=votes,
                video=video,  # type: ignore[arg-type]
                caption_body=caption_body,
                now=now_ist(),
            )
    return store.kit_out(kit, r2)


@router.get("/kits/{kit_id}")
async def get_kit(channel_id: str, kit_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    _, r2 = _deps()
    async with _errors():
        return store.kit_out(await store.get_kit(db, channel_id, kit_id), r2)


@router.post("/kits/{kit_id}/check")
async def check_now(channel_id: str, kit_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    """ "I've posted it": look for the reel now instead of at the next check."""
    _, r2 = _deps()
    graph, ig_user_id, own = await _graph(db, channel_id)
    async with _errors():
        await store.get_kit(db, channel_id, kit_id)
        await store.detect(db, graph, channel_id, ig_user_id, now_ist())
        kit = await store.get_kit(db, channel_id, kit_id)
        if kit["status"] == "live":
            kit = await store.advance(db, graph, kit, own, now_ist())
        return store.kit_out(kit, r2)


@router.post("/kits/{kit_id}/link")
async def link_kit(
    channel_id: str, kit_id: str, body: LinkIn, db: AsyncIOMotorDatabase = Depends(get_db)
) -> dict[str, Any]:
    """Tie the kit to a post the detector didn't recognise (say, an edited caption)."""
    _, r2 = _deps()
    graph, ig_user_id, _ = await _graph(db, channel_id)
    async with _errors():
        kit = await store.get_kit(db, channel_id, kit_id)
        return store.kit_out(await store.link_by_hand(db, graph, kit, ig_user_id, body.media_id, now_ist()), r2)


@router.post("/kits/{kit_id}/retry")
async def retry_kit(channel_id: str, kit_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    _, r2 = _deps()
    async with _errors():
        kit = await store.get_kit(db, channel_id, kit_id)
        return store.kit_out(await store.retry_failed(db, kit, now_ist()), r2)


@router.post("/kits/{kit_id}/cancel")
async def cancel_kit(channel_id: str, kit_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    _, r2 = _deps()
    async with _errors():
        kit = await store.get_kit(db, channel_id, kit_id)
        return store.kit_out(await store.cancel_kit(db, kit, now_ist()), r2)
