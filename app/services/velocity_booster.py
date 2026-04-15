"""Background service for the 'Velocity Booster' automation.

This service runs every hour and checks the views of the most recently published video.
If a video has been public for >= X hours but has < Y views, it automatically
pulls forward the next video from the posting queue and schedules it for
immediate-ish release (e.g. 15 minutes from now).

This helps maintain channel momentum when a video underperforms.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.timezone import now_ist, to_ist_iso
from app.database import update_channel_task_status
from app.services.schedule_operation import (
    enqueue_video_for_youtube,
    schedule_single_video_instagram,
)

logger = get_logger(__name__)

_POLL_INTERVAL_SECONDS = 3600  # 1 hour


async def _refresh_last_video_stats(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    video_doc: dict[str, Any],
    platform: str,
) -> int:
    """Fetch live views for the specific video and update the DB doc.
    
    Returns the updated view count.
    """
    from app.routers.videos import sync_videos
    
    video_id = video_doc["video_id"]
    logger.info("Velocity Booster: refreshing stats for last video '%s' (channel: %s)", video_id, channel_id)
    
    try:
        # We use the existing sync_videos logic which handles both platforms
        # By passing video_id filter, we'd need to modify sync_videos, 
        # but for now we'll just run a full sync for the channel as it's safer.
        await sync_videos(channel_id=channel_id, db=db, body=None)
        
        # Re-fetch the updated video doc
        updated_doc = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
        views = (updated_doc.get("metadata", {}) or {}).get("views", 0)
        return views or 0
    except Exception as e:
        logger.error("Velocity Booster: failed to refresh stats for video %s: %s", video_id, e)
        # Fallback to current DB value
        return (video_doc.get("metadata", {}) or {}).get("views", 0) or 0


async def process_velocity_booster_for_channel(
    db: AsyncIOMotorDatabase,
    channel_doc: dict[str, Any],
) -> None:
    """Check conditions and trigger an auto-schedule if necessary."""
    channel_id = channel_doc["channel_id"]
    platform = channel_doc.get("platform", "youtube")
    
    config = channel_doc.get("automation_config", {}).get("velocity_booster", {})
    if not config.get("enabled"):
        return

    min_hours = config.get("min_hours_since_last_upload", 12)
    min_views = config.get("min_views_threshold", 1000)
    delay_min = config.get("schedule_delay_minutes", 15)

    # 1. Find the most recently published video
    last_published = await db.videos.find_one(
        {"channel_id": channel_id, "status": "published"},
        sort=[("published_at", -1)]
    )

    if not last_published:
        logger.info("Velocity Booster: no published videos found for channel '%s' — skipping", channel_id)
        return

    published_at = last_published.get("published_at")
    if not published_at:
        return

    now = now_ist()
    time_since_upload = now - published_at

    if time_since_upload < timedelta(hours=min_hours):
        logger.info(
            "Velocity Booster: last video on '%s' is only %s old (threshold: %dh) — skipping",
            channel_id, time_since_upload, min_hours
        )
        return

    # 2. Get current views (Live Refresh)
    views = await _refresh_last_video_stats(db, channel_id, last_published, platform)
    
    if views >= min_views:
        logger.info(
            "Velocity Booster: last video on '%s' has %d views (threshold: %d) — pace is healthy",
            channel_id, views, min_views
        )
        return

    logger.warning(
        "Velocity Booster TRIGGERED for '%s': last video has only %d views after %s. Boosting pace!",
        channel_id, views, time_since_upload
    )

    # 3. Pull next video from posting queue
    next_up = await db.posting_queue.find_one(
        {"channel_id": channel_id},
        sort=[("position", 1)]
    )

    if not next_up:
        logger.error("Velocity Booster: TRIGGERED but posting_queue is empty for channel '%s'!", channel_id)
        return

    video_id = next_up["video_id"]
    video_doc = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if not video_doc:
        logger.error("Velocity Booster: queue entry refers to missing video %s", video_id)
        return

    # 4. Schedule for immediate release
    scheduled_at = now + timedelta(minutes=delay_min)
    
    try:
        if platform == "youtube":
            await enqueue_video_for_youtube(
                db=db,
                channel_id=channel_id,
                video_doc=video_doc,
                scheduled_at=scheduled_at,
            )
        else:
            await schedule_single_video_instagram(
                db=db,
                channel_id=channel_id,
                video_doc=video_doc,
                scheduled_at=scheduled_at,
            )
        
        logger.success(
            "Velocity Booster: successfully scheduled boost video '%s' for %s",
            video_doc.get("title", video_id),
            to_ist_iso(scheduled_at)
        )
        
        await update_channel_task_status(db, channel_id, "velocity_booster")
        
    except Exception:
        logger.exception("Velocity Booster: failed to schedule boost video for channel %s", channel_id)


async def run_velocity_booster(db: AsyncIOMotorDatabase) -> None:
    """Infinite loop for the Velocity Booster automation."""
    logger.info("Velocity Booster service started (poll interval: %ds)", _POLL_INTERVAL_SECONDS)

    while True:
        try:
            # We process all channels that have the automation enabled
            channels = await db.channels.find(
                {"automation_config.velocity_booster.enabled": True}
            ).to_list(length=None)

            if channels:
                logger.info("Velocity Booster: checking %d enabled channel(s)", len(channels))
                for channel in channels:
                    await process_velocity_booster_for_channel(db, channel)
            else:
                logger.info("Velocity Booster: no channels have this automation enabled.")

        except asyncio.CancelledError:
            logger.info("Velocity Booster service shutting down")
            break
        except Exception:
            logger.exception("Velocity Booster service encountered an error during poll cycle")

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
