"""Posting router – manage the ready queue, scheduled queue, and YouTube scheduling."""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/posting",
    tags=["posting"],
    dependencies=[Depends(verify_api_key)],
)


def _get_services(channel_id: str):
    """Lazy import to avoid circular dependency."""
    from app.main import r2_service, youtube_service_manager  # type: ignore[import]

    youtube_service = youtube_service_manager.get_service(channel_id) if youtube_service_manager else None
    return r2_service, youtube_service


# ------------------------------------------------------------------
# GET /queue  –  current scheduled queue
# ------------------------------------------------------------------


from pydantic import BaseModel, Field

class QueueItem(BaseModel):
    position: int
    video_id: str
    added_at: datetime
    scheduled_at: Optional[datetime] = None
    title: str = ""
    category: str = ""

@router.get("/queue", response_model=list[QueueItem])
async def get_queue(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return the current scheduled queue sorted by position."""
    queue = (
        await db.schedule_queue.find({"channel_id": channel_id})
        .sort("position", 1)
        .to_list(length=None)
    )

    result = []
    for entry in queue:
        video = await db.videos.find_one(
            {"channel_id": channel_id, "video_id": entry["video_id"]}
        )
        item = {
            "position": entry["position"],
            "video_id": entry["video_id"],
            "added_at": entry["added_at"],
            "scheduled_at": entry.get("scheduled_at"),
        }
        if video:
            item["title"] = video.get("title", "")
            item["category"] = video.get("category", "")
        result.append(item)

    return result


# ------------------------------------------------------------------
# POST /schedule-all  –  schedule all ready videos on YouTube
# ------------------------------------------------------------------


@router.post("/schedule-all")
async def schedule_all(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Schedule every video in the **ready queue** on YouTube.

    For each video in the ready queue:
    1. Computes a publish slot from ``best_posting_times``.
    2. Uploads to YouTube as private with ``publishAt``.
    3. On success: removes from ready queue, adds to scheduled queue,
       status → ``scheduled``.

    Uses the same core operation as the schedule endpoint.
    """
    from app.config import get_settings
    from app.services.scheduler import compute_schedule_slots
    from app.services.schedule_operation import schedule_single_video

    settings = get_settings()
    r2_service, youtube_service = _get_services(channel_id)

    if r2_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="R2 service not initialised",
        )
    if youtube_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No YouTube token for channel '{channel_id}'. Run: python generate_youtube_token.py {channel_id}",
        )

    # ---- Collect videos from the ready queue ----
    posting_entries = (
        await db.posting_queue.find({"channel_id": channel_id})
        .sort("position", 1)
        .to_list(length=None)
    )

    if not posting_entries:
        return {"ok": True, "scheduled": 0, "failed": 0, "details": []}

    videos_to_schedule = []
    for entry in posting_entries:
        v = await db.videos.find_one(
            {"channel_id": channel_id, "video_id": entry["video_id"]}
        )
        if v and v.get("status") == "ready":
            videos_to_schedule.append(v)

    if not videos_to_schedule:
        return {"ok": True, "scheduled": 0, "failed": 0, "details": []}

    # ---- Fetch best_posting_times from the latest analysis ----
    analysis = await db.analysis.find_one({"channel_id": channel_id})
    if not analysis or not analysis.get("best_posting_times"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No analysis with best_posting_times found — run analysis first",
        )

    # ---- Gather already-occupied slots ----
    existing_scheduled = await db.schedule_queue.find(
        {"channel_id": channel_id}
    ).to_list(length=None)
    occupied_datetimes = [e.get("scheduled_at") for e in existing_scheduled]

    # ---- Compute publish slots ----
    slots = compute_schedule_slots(
        best_posting_times=analysis["best_posting_times"],
        occupied_datetimes=occupied_datetimes,
        num_videos=len(videos_to_schedule),
        timezone_str=settings.TIMEZONE,
    )

    if len(slots) < len(videos_to_schedule):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could only find {len(slots)} available slots for {len(videos_to_schedule)} videos",
        )

    # ---- Schedule each video (upload to YouTube) ----
    scheduled = 0
    failed = 0
    details: list[dict] = []

    for video_doc, slot_dt in zip(videos_to_schedule, slots):
        result = await schedule_single_video(
            db=db,
            r2_service=r2_service,
            youtube_service=youtube_service,
            channel_id=channel_id,
            video_doc=video_doc,
            scheduled_at=slot_dt,
        )
        details.append(result)
        if result["status"] == "scheduled":
            scheduled += 1
        else:
            failed += 1

    return {"ok": True, "scheduled": scheduled, "failed": failed, "details": details}
