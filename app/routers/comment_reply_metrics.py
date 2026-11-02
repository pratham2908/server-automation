"""Metrics for the Auto-replies & review queue tab."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
from app.services.comment_reply_metrics import PENDING, SERIES_DAYS, reply_metrics
from app.timezone import now_ist

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/comment-replies",
    tags=["comment-replies"],
    dependencies=[Depends(verify_api_key)],
)

WINDOWS: dict[str, int | None] = {"24h": 1, "7d": 7, "30d": 30, "all": None}

_REPLY_FIELDS = {
    "comment_id": 1,
    "status": 1,
    "sentiment": 1,
    "replied_at": 1,
    "approved": 1,
    "edited": 1,
    "last_error": 1,
    "video_title": 1,
    "comment_published_at": 1,
}
_QUEUE_FIELDS = {"comment_id": 1, "status": 1, "skip_reason": 1, "created_at": 1, "commented_at": 1}


@router.get("/metrics")
async def comment_reply_metrics(
    channel_id: str,
    window: Literal["24h", "7d", "30d", "all"] = Query("7d"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """Comments in, replies sent (auto vs approved), skips by reason, reply time, sentiment, top reels and
    a 14-day series, for the chosen window. Pending drafts are counted as they stand now."""
    now = now_ist()
    days = WINDOWS[window]
    # Rows for the window, but never less than the series needs.
    since = (now - timedelta(days=max(days or 0, SERIES_DAYS) + 1)).replace(tzinfo=None) if days else None
    reply_query: dict[str, Any] = {"channel_id": channel_id}
    queue_query: dict[str, Any] = {"channel_id": channel_id}
    if since is not None:
        reply_query["$or"] = [{"replied_at": {"$gte": since}}, {"status": PENDING}]
        queue_query["created_at"] = {"$gte": since}
    replies = await db.comment_replies.find(reply_query, _REPLY_FIELDS).to_list(length=None)
    queue = await db.comment_ai_reply_queue.find(queue_query, _QUEUE_FIELDS).to_list(length=None)
    return {"window": window, **reply_metrics(replies, queue, now, days)}
