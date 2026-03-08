"""Core schedule operation: upload a video to YouTube and update all DB state.

The ``schedule`` endpoint (videos router) delegates to
:func:`schedule_single_video` so the logic lives in one place.
"""

import os
from datetime import datetime
from typing import Any

import pytz

from app.logger import get_logger
from app.timezone import now_ist

logger = get_logger(__name__)


async def schedule_single_video(
    *,
    db,
    r2_service,
    youtube_service,
    channel_id: str,
    video_doc: dict[str, Any],
    scheduled_at: datetime,
) -> dict[str, Any]:
    """Upload a single video to YouTube with a ``publishAt`` time.

    On success the function:
    1. Sets ``youtube_video_id`` and status → ``scheduled`` on the video doc.
    2. Removes the video from the ready queue (``posting_queue``).
    3. Inserts an entry into the scheduled queue (``schedule_queue``).

    Returns a result dict with ``"status"`` of ``"scheduled"`` or ``"failed"``.
    """
    video_id = video_doc["video_id"]

    if not video_doc.get("r2_object_key"):
        return {
            "video_id": video_id,
            "status": "skipped",
            "reason": "no R2 key",
        }

    # Convert scheduled_at to UTC ISO string for YouTube's publishAt.
    if scheduled_at.tzinfo is not None:
        utc_dt = scheduled_at.astimezone(pytz.utc)
    else:
        utc_dt = scheduled_at
    publish_at_str = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    tmp_path = None
    try:
        tmp_path = r2_service.download_video(video_doc["r2_object_key"])

        yt_id = youtube_service.upload_video(
            file_path=tmp_path,
            title=video_doc.get("title", ""),
            description=video_doc.get("description", ""),
            tags=video_doc.get("tags", []),
            publish_at=publish_at_str,
        )

        now = now_ist()

        # Update video record.
        await db.videos.update_one(
            {"channel_id": channel_id, "video_id": video_id},
            {
                "$set": {
                    "youtube_video_id": yt_id,
                    "status": "scheduled",
                    "scheduled_at": scheduled_at,
                    "updated_at": now,
                }
            },
        )

        # Remove from ready queue.
        await db.posting_queue.delete_one(
            {"channel_id": channel_id, "video_id": video_id}
        )

        # Determine next position in scheduled queue.
        last = await db.schedule_queue.find_one(
            {"channel_id": channel_id},
            sort=[("position", -1)],
        )
        next_pos = (last["position"] + 1) if last else 1

        # Insert into scheduled queue.
        await db.schedule_queue.insert_one(
            {
                "channel_id": channel_id,
                "video_id": video_id,
                "position": next_pos,
                "scheduled_at": scheduled_at,
                "added_at": now,
            }
        )

        logger.success(
            "Scheduled '%s' on YouTube (yt_id=%s) for %s",
            video_doc.get("title", video_id),
            yt_id,
            publish_at_str,
        )

        return {
            "video_id": video_id,
            "status": "scheduled",
            "youtube_video_id": yt_id,
            "scheduled_at": scheduled_at.isoformat(),
        }

    except Exception:
        logger.exception("Failed to schedule video %s on YouTube", video_id)
        return {"video_id": video_id, "status": "failed"}

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
