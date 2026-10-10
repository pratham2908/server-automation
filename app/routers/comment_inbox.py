"""Comment inbox: a channel's comment threads, an AI draft for any message, and sending a reply.

Reads go to the platform live (the threads change faster than anything we could
cache without lying), so each call costs a platform request.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger
from app.services.comment_inbox import (
    ThreadNotFoundError,
    draft_reply,
    list_threads,
    load_thread,
    platform_video_id,
    send_reply,
)
from app.services.instagram import graph_error_message
from app.services.instagram_tokens import (
    PREFER_FOR_READING_COMMENTS,
    PREFER_FOR_REPLYING,
    PROVIDER_INSTAGRAM,
    provider_of,
)

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/comment-inbox",
    tags=["comment-inbox"],
    dependencies=[Depends(verify_api_key)],
)

DEFAULT_THREADS = 30
MAX_THREADS = 100

NO_COMMENT_READ_WARNING = (
    "This channel only has an Instagram Login token, which cannot read comments (Instagram returns an empty "
    "list). Add a Facebook Login token to the channel to see them."
)


class MessageOut(BaseModel):
    comment_id: str
    author: str
    text: str
    published_at: str
    like_count: int = 0
    avatar_url: str | None = None
    is_own: bool = Field(description="Written by this channel")


class ThreadOut(BaseModel):
    thread_id: str = Field(description="The opening comment's id; replies are posted under it")
    video_id: str
    platform: str
    message: MessageOut
    replies: list[MessageOut]
    reply_count: int
    last_activity: str
    needs_reply: bool = Field(description="The last message in the thread is not ours")
    comment_url: str = ""
    pending_draft: str | None = Field(None, description="A reply the auto-reply cycle drafted and nobody has sent")


class ThreadsOut(BaseModel):
    video_id: str
    video_title: str
    threads: list[ThreadOut]
    warning: str | None = None


class DraftIn(BaseModel):
    video_id: str
    target_comment_id: str | None = Field(
        None, description="Which message to answer; default is the newest viewer message"
    )
    instruction: str = Field("", max_length=500, description="Optional steer, e.g. 'shorter' or 'answer in Hindi'")


class DraftOut(BaseModel):
    text: str
    target_comment_id: str


class SendIn(BaseModel):
    video_id: str
    text: str
    target_comment_id: str | None = None


class SendOut(BaseModel):
    ok: bool = True
    reply_id: str
    text: str


async def _channel_and_video(
    db: AsyncIOMotorDatabase, channel_id: str, video_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found")
    video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if not video:
        raise HTTPException(status_code=404, detail=f"Video '{video_id}' not found on this channel")
    return dict(channel), dict(video)


async def _service(channel: dict[str, Any], prefer: str) -> Any:
    """The platform client for this channel; Instagram can hold two tokens, so *prefer* picks one."""
    import app.main as main_mod

    service: Any = None
    if channel.get("platform", "youtube") == "youtube":
        manager = main_mod.youtube_service_manager
        service = await manager.get_service(channel["channel_id"]) if manager else None
    else:
        ig_manager = main_mod.instagram_service_manager
        service = await ig_manager.get_service(channel["channel_id"], prefer) if ig_manager else None
    if not service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No {channel.get('platform', 'youtube')} client available for '{channel['channel_id']}'",
        )
    return service


def _platform_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"The platform refused the request: {graph_error_message(exc)[:300]}",
    )


@router.get("/threads", response_model=ThreadsOut)
async def get_threads(
    channel_id: str,
    video_id: str = Query(..., description="Our video id"),
    limit: int = Query(DEFAULT_THREADS, ge=1, le=MAX_THREADS),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    channel, video = await _channel_and_video(db, channel_id, video_id)
    if not platform_video_id(video, channel.get("platform", "youtube")):
        raise HTTPException(
            status_code=422, detail="This video has not been published to the platform, so it has no comments"
        )
    reader = await _service(channel, PREFER_FOR_READING_COMMENTS)
    try:
        threads = await list_threads(db, channel, reader, video, limit)
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller with the platform's own words
        logger.warning("Reading comments for '%s' failed: %s", video_id, exc)
        raise _platform_error(exc)
    warning = (
        NO_COMMENT_READ_WARNING
        if channel.get("platform") == "instagram" and getattr(reader, "_provider", "") == PROVIDER_INSTAGRAM
        else None
    )
    return ThreadsOut(
        video_id=video_id,
        video_title=video.get("title", ""),
        threads=[ThreadOut(**t) for t in threads],
        warning=warning,
    )


@router.post("/threads/{thread_id}/draft", response_model=DraftOut)
async def draft_thread_reply(
    channel_id: str, thread_id: str, body: DraftIn, db: AsyncIOMotorDatabase = Depends(get_db)
):
    """An AI draft of the channel's next message, given the entire thread. Nothing is posted."""
    import app.main as main_mod

    channel, video = await _channel_and_video(db, channel_id, body.video_id)
    reader = await _service(channel, PREFER_FOR_READING_COMMENTS)
    if not main_mod.gemini_service:
        raise HTTPException(status_code=503, detail="The AI service is unavailable")
    try:
        thread = await load_thread(channel, reader, video, thread_id)
        draft = await draft_reply(main_mod.gemini_service, thread, video, body.target_comment_id, body.instruction)
    except ThreadNotFoundError:
        raise HTTPException(status_code=404, detail="That comment is no longer on the platform")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Drafting a thread reply failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Could not draft a reply: {graph_error_message(exc)[:300]}")
    if not draft["text"]:
        raise HTTPException(status_code=502, detail="The AI returned no reply. Try again, or add a direction.")
    return DraftOut(**draft)


@router.post("/threads/{thread_id}/reply", response_model=SendOut)
async def send_thread_reply(channel_id: str, thread_id: str, body: SendIn, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Post a reply under the thread's opening comment and record it."""
    channel, video = await _channel_and_video(db, channel_id, body.video_id)
    reader = await _service(channel, PREFER_FOR_READING_COMMENTS)
    poster = await _service(channel, PREFER_FOR_REPLYING)
    try:
        thread = await load_thread(channel, reader, video, thread_id)
        sent = await send_reply(db, channel, poster, thread, video, body.text, body.target_comment_id)
    except ThreadNotFoundError:
        raise HTTPException(status_code=404, detail="That comment is no longer on the platform")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sending a thread reply failed: %s", exc)
        raise _platform_error(exc)
    return SendOut(**sent)
