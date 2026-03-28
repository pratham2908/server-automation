"""Comment replies router -- manual trigger, reply history, and config."""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger
from app.timezone import now_ist

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/comment-replies",
    tags=["comment-replies"],
    dependencies=[Depends(verify_api_key)],
)

config_router = APIRouter(
    prefix="/api/v1/comment-replies/config",
    tags=["comment-replies"],
    dependencies=[Depends(verify_api_key)],
)


# ------------------------------------------------------------------
# POST /trigger  --  manually run a reply cycle
# ------------------------------------------------------------------


@router.post("/trigger", status_code=status.HTTP_200_OK)
async def trigger_comment_replies(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Manually trigger a comment reply cycle for this channel."""
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found")

    import app.main as main_mod
    from app.services.comment_reply_engine import run_comment_reply_cycle

    stats = await run_comment_reply_cycle(
        channel_id=channel_id,
        db=db,
        youtube_service_manager=main_mod.youtube_service_manager,
        instagram_service_manager=main_mod.instagram_service_manager,
        gemini_service=main_mod.gemini_service,
    )

    return {"ok": True, "channel_id": channel_id, **stats}


# ------------------------------------------------------------------
# GET /history  --  list replied comments
# ------------------------------------------------------------------


@router.get("/history")
async def get_reply_history(
    channel_id: str,
    video_id: Optional[str] = Query(None, description="Filter by video_id"),
    status: Optional[str] = Query(None, description="Filter by status (replied, skipped_negative, etc)"),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List comments that have been processed for this channel."""
    query: dict[str, Any] = {"channel_id": channel_id}
    if video_id:
        query["video_id"] = video_id
    if status:
        query["status"] = status

    docs = await db.comment_replies.find(query).sort(
        "replied_at", -1
    ).limit(limit).to_list(length=limit)

    for d in docs:
        d.pop("_id", None)

    return docs


# ------------------------------------------------------------------
# GET /config  --  read reply config
# ------------------------------------------------------------------


class CommentReplyConfigUpdate(BaseModel):
    enabled: Optional[bool] = Field(None, description="Enable or disable auto-replies")
    reply_templates: Optional[list[str]] = Field(None, description="Reply message templates")
    max_replies_per_run: Optional[int] = Field(None, ge=1, le=200)
    max_videos_per_run: Optional[int] = Field(None, ge=1, le=50)
    video_recency_days: Optional[int] = Field(None, ge=1, le=365)
    interval_hours: Optional[int] = Field(None, ge=1, le=168, description="Cron interval in hours")


@config_router.get("/")
async def get_comment_reply_config(
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return the current comment reply configuration."""
    doc = await db.config.find_one({"key": "comment_reply_config"})
    if doc:
        doc.pop("_id", None)
        return doc

    return {
        "key": "comment_reply_config",
        "enabled": True,
        "reply_templates": [
            "Thanks so much! Subscribe so you don't miss more content like this!",
            "Glad you enjoyed it! Hit subscribe for more!",
            "Thank you! Don't forget to subscribe for more!",
        ],
        "max_replies_per_run": 50,
        "max_videos_per_run": 10,
        "video_recency_days": 30,
        "interval_hours": 6,
        "description": "Default — not yet customised.",
    }


# ------------------------------------------------------------------
# PUT /config  --  update reply config
# ------------------------------------------------------------------


@config_router.put("/")
async def update_comment_reply_config(
    body: CommentReplyConfigUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Update the comment reply configuration. Only provided fields are changed."""
    updates: dict[str, Any] = {}
    for field, value in body.model_dump(exclude_none=True).items():
        updates[field] = value

    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    updates["updated_at"] = now_ist()

    await db.config.update_one(
        {"key": "comment_reply_config"},
        {"$set": updates, "$setOnInsert": {"key": "comment_reply_config"}},
        upsert=True,
    )

    doc = await db.config.find_one({"key": "comment_reply_config"})
    doc.pop("_id", None)
    return {"ok": True, **doc}
