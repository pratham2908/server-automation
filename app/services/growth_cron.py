from __future__ import annotations

"""Background cron loop for daily channel growth snapshots.

Runs every 24 hours (configurable via ``growth_tracking_config``
in the ``config`` collection).  Iterates over all channels, 
fetches current stats, and records a snapshot.
"""

import asyncio
from typing import Any
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.logger import get_logger
from app.database import update_channel_task_status
from app.services.growth_tracking import GrowthTrackingService

logger = get_logger(__name__)

_DEFAULT_INTERVAL_HOURS = 24

async def _get_interval_seconds(db: AsyncIOMotorDatabase) -> float:
    doc = await db.config.find_one({"key": "growth_tracking_config"})
    hours = _DEFAULT_INTERVAL_HOURS
    if doc:
        hours = doc.get("interval_hours", _DEFAULT_INTERVAL_HOURS)
    return float(hours) * 3600

async def run_growth_tracking_cron(
    db: AsyncIOMotorDatabase,
    youtube_service_manager: Any,
    instagram_service_manager: Any,
) -> None:
    """Infinite loop — sleeps for the configured interval, runs snapshot cycles, repeats."""

    logger.info("Growth tracking cron started (default interval: %dh)", _DEFAULT_INTERVAL_HOURS)
    growth_service = GrowthTrackingService(db)

    # Initial delay to avoid hitting APIs right after startup if needed, 
    # but usually we want a run soon. 
    # Let's wait a small amount of time to let other systems initialize.
    await asyncio.sleep(60)

    while True:
        try:
            from app.services.metrics import metrics_service
            metrics_service.track_task_start("growth_tracking")
            
            channels = await db.channels.find().to_list(length=None)
            logger.info(f"Growth tracking cron tick — processing {len(channels)} channel(s)")

            for channel in channels:
                channel_id = channel.get("channel_id")
                platform = channel.get("platform", "youtube")
                if not channel_id:
                    continue

                try:
                    subs, views = 0, 0
                    metadata = {}
                    
                    if platform == "youtube":
                        yt_service = await youtube_service_manager.get_service(channel_id)
                        if yt_service:
                            info = yt_service.get_channel_info(channel.get("youtube_channel_id", ""))
                            subs = info.get("subscriber_count", 0)
                            views = info.get("view_count", 0)
                            metadata = {"video_count": info.get("video_count", 0)}
                    
                    elif platform == "instagram":
                        ig_service = await instagram_service_manager.get_service(channel_id)
                        if ig_service:
                            info = ig_service.get_account_info(channel.get("instagram_user_id", ""))
                            subs = info.get("followers_count", 0)
                            # For Instagram, views aren't a single account-level metric.
                            # We could aggregate them here if we find it helpful.
                            views = 0 
                            metadata = {"media_count": info.get("media_count", 0)}

                    # Record the snapshot
                    await growth_service.record_snapshot(channel_id, platform, subs, views, metadata)
                    
                    # Update channel status
                    await update_channel_task_status(db, channel_id, "growth_tracking")
                    
                except Exception as exc:
                    logger.error(f"Growth snapshot failed for channel '{channel_id}': {exc}")

            metrics_service.track_task_end("growth_tracking", "success")
        except Exception as exc:
            logger.error(f"Growth tracking cron top-level error: {exc}")
            metrics_service.track_task_end("growth_tracking", "error")

        interval = await _get_interval_seconds(db)
        logger.info(f"Growth tracking cron: sleeping {interval / 3600:.1f} hours until next run")
        await asyncio.sleep(interval)
