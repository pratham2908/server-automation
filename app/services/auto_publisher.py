"""Background auto-publisher for Instagram Reels.

Instagram has no native ``publishAt`` like YouTube, so scheduled reels
sit in the ``schedule_queue`` until their ``scheduled_at`` time arrives.
This module runs an async loop that polls every 5 minutes and publishes
any due reels.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from app.logger import get_logger
from app.services.schedule_operation import _build_instagram_caption
from app.database import update_channel_task_status
from app.timezone import now_ist

logger = get_logger(__name__)

_POLL_INTERVAL_SECONDS = 300  # 5 minutes
_MAX_PUBLISH_ATTEMPTS = 5


async def _publish_one_reel(
    *,
    db: Any,
    r2_service: Any,
    instagram_service: Any,
    channel_doc: dict[str, Any],
    video_doc: dict[str, Any],
    queue_entry: dict[str, Any],
) -> bool:
    """Upload and publish a single reel.  Returns ``True`` on success."""
    channel_id = channel_doc["channel_id"]
    video_id = video_doc["video_id"]
    ig_user_id = channel_doc.get("instagram_user_id", "")

    if not ig_user_id:
        logger.error("Channel '%s' has no instagram_user_id", channel_id)
        return False

    r2_key = video_doc.get("r2_object_key")
    if not r2_key:
        logger.error("Video '%s' has no r2_object_key — skipping", video_id)
        return False

    tmp_path = None
    try:
        caption = _build_instagram_caption(video_doc)

        tmp_path = r2_service.download_video(r2_key)

        media_id = instagram_service.publish_reel(
            ig_user_id=ig_user_id,
            file_path=tmp_path,
            caption=caption,
        )

        now = now_ist()

        await db.videos.update_one(
            {"channel_id": channel_id, "video_id": video_id},
            {
                "$set": {
                    "instagram_media_id": media_id,
                    "status": "published",
                    "published_at": now,
                    "updated_at": now,
                }
            },
        )

        await db.schedule_queue.delete_one({"_id": queue_entry["_id"]})

        logger.success(
            "Auto-published reel '%s' (media_id=%s) for channel '%s'",
            video_doc.get("title", video_id)[:50],
            media_id,
            channel_id,
        )
        
        # Update channel task history
        await update_channel_task_status(db, channel_id, "auto_publisher")
        
        return True

    except Exception:
        attempts = queue_entry.get("attempt_count", 0) + 1
        logger.exception(
            "Auto-publisher failed for video '%s' on channel '%s' (attempt %d/%d)",
            video_id,
            channel_id,
            attempts,
            _MAX_PUBLISH_ATTEMPTS,
        )
        if attempts >= _MAX_PUBLISH_ATTEMPTS:
            logger.error(
                "Auto-publisher: giving up on video '%s' after %d attempts — marking failed",
                video_id,
                _MAX_PUBLISH_ATTEMPTS,
            )
            await db.schedule_queue.delete_one({"_id": queue_entry["_id"]})
            await db.videos.update_one(
                {"channel_id": channel_id, "video_id": video_id},
                {"$set": {"status": "ready", "updated_at": now_ist()}},
            )
        else:
            await db.schedule_queue.update_one(
                {"_id": queue_entry["_id"]},
                {"$set": {"attempt_count": attempts}},
            )
        return False

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def run_auto_publisher(db: Any, r2_service: Any) -> None:
    """Long-running loop that publishes due Instagram reels every 5 minutes."""
    from app.services.instagram import InstagramServiceManager

    logger.info("Auto-publisher started (poll interval: %ds)", _POLL_INTERVAL_SECONDS)
    from app.services.metrics import metrics_service

    while True:
        try:
            metrics_service.track_task_start("auto_publisher")
            await _poll_and_publish(db, r2_service)
            metrics_service.track_task_end("auto_publisher", "success")
        except asyncio.CancelledError:
            logger.info("Auto-publisher shutting down")
            break
        except Exception:
            logger.exception("Auto-publisher encountered an error during poll cycle")
            metrics_service.track_task_end("auto_publisher", "error")

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def _poll_and_publish(db: Any, r2_service: Any) -> None:
    """One poll cycle: find due entries and publish them."""
    from app.main import instagram_service_manager  # type: ignore[import]

    now = now_ist()

    due_entries = await db.schedule_queue.find(
        {"scheduled_at": {"$lte": now}}
    ).to_list(length=None)

    if not due_entries:
        return

    for entry in due_entries:
        channel_id = entry.get("channel_id", "")
        video_id = entry.get("video_id", "")

        channel_doc = await db.channels.find_one({"channel_id": channel_id})
        if not channel_doc:
            logger.warning("Auto-publisher: channel '%s' not found — removing queue entry", channel_id)
            await db.schedule_queue.delete_one({"_id": entry["_id"]})
            continue

        platform = channel_doc.get("platform", "youtube")
        if platform != "instagram":
            continue

        video_doc = await db.videos.find_one(
            {"channel_id": channel_id, "video_id": video_id}
        )
        if not video_doc:
            logger.warning("Auto-publisher: video '%s' not found — removing queue entry", video_id)
            await db.schedule_queue.delete_one({"_id": entry["_id"]})
            continue

        if video_doc.get("status") != "scheduled":
            logger.warning(
                "Auto-publisher: video '%s' status is '%s', not 'scheduled' — removing stale queue entry",
                video_id, video_doc.get("status"),
            )
            await db.schedule_queue.delete_one({"_id": entry["_id"]})
            continue

        if not instagram_service_manager:
            logger.error("Auto-publisher: InstagramServiceManager not available")
            continue

        ig_service = await instagram_service_manager.get_service(channel_id)
        if not ig_service:
            logger.error("Auto-publisher: no Instagram service for channel '%s'", channel_id)
            continue

        await _publish_one_reel(
            db=db,
            r2_service=r2_service,
            instagram_service=ig_service,
            channel_doc=channel_doc,
            video_doc=video_doc,
            queue_entry=entry,
        )
