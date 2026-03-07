"""Posting router – manage the posting queue and upload videos to YouTube."""

import logging
import os
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


def _get_services():
    """Lazy import to avoid circular dependency."""
    from app.main import r2_service, youtube_service  # type: ignore[import]

    return r2_service, youtube_service


# ------------------------------------------------------------------
# GET /queue  –  current posting queue
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
    """Return the current posting queue sorted by position."""
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
# POST /upload-all  –  upload all queued videos to YouTube
# ------------------------------------------------------------------


@router.post("/upload-all")
async def upload_all(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Download each queued video from R2 and upload to YouTube.

    Processes the queue in order. Returns a summary of successes and
    failures.
    """
    r2_service, youtube_service = _get_services()

    if r2_service is None or youtube_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="R2 or YouTube service not initialised",
        )

    queue = (
        await db.schedule_queue.find({"channel_id": channel_id})
        .sort("position", 1)
        .to_list(length=None)
    )

    if not queue:
        return {"ok": True, "uploaded": 0, "failed": 0, "details": []}

    uploaded = 0
    failed = 0
    details: list[dict] = []

    for entry in queue:
        video_id = entry["video_id"]
        video = await db.videos.find_one(
            {"channel_id": channel_id, "video_id": video_id}
        )

        if not video or not video.get("r2_object_key"):
            details.append({"video_id": video_id, "status": "skipped", "reason": "no R2 key"})
            failed += 1
            continue

        tmp_path = None
        try:
            tmp_path = r2_service.download_video(video["r2_object_key"])

            # Convert scheduled_at to UTC ISO string for YouTube's publishAt.
            publish_at_str = None
            scheduled_at = entry.get("scheduled_at")
            if scheduled_at:
                if scheduled_at.tzinfo is not None:
                    import pytz
                    utc_dt = scheduled_at.astimezone(pytz.utc)
                else:
                    utc_dt = scheduled_at
                publish_at_str = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            yt_id = youtube_service.upload_video(
                file_path=tmp_path,
                title=video.get("title", ""),
                description=video.get("description", ""),
                tags=video.get("tags", []),
                publish_at=publish_at_str,
            )

            # Update video record with YouTube ID and mark published.
            await db.videos.update_one(
                {"channel_id": channel_id, "video_id": video_id},
                {
                    "$set": {
                        "youtube_video_id": yt_id,
                        "status": "published",
                        "updated_at": datetime.utcnow(),
                    }
                },
            )

            # Remove from schedule queue.
            await db.schedule_queue.delete_one({"_id": entry["_id"]})

            details.append({"video_id": video_id, "status": "uploaded", "youtube_video_id": yt_id})
            uploaded += 1

        except Exception:
            logger.exception("Failed to upload video %s", video_id)
            details.append({"video_id": video_id, "status": "failed"})
            failed += 1

        finally:
            # Clean up temp file.
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    return {"ok": True, "uploaded": uploaded, "failed": failed, "details": details}
