"""Background cron loop for daily channel growth snapshots.

Runs every 24 hours (configurable via ``growth_tracking_config``
in the ``config`` collection).  Iterates over all channels,
fetches current stats, and records a snapshot.
"""

from __future__ import annotations

import asyncio
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import not_paused_query
from app.logger import get_logger
from app.services.channel_stats import refresh_channel_stats
from app.services.error_reporting import report_error
from app.services.growth_tracking import GrowthTrackingService
from app.services.metrics import metrics_service

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
            metrics_service.track_task_start("growth_tracking")

            channels = await db.channels.find(not_paused_query()).to_list(length=None)
            logger.info(f"Growth tracking cron tick — processing {len(channels)} channel(s)")

            for channel in channels:
                channel_id = channel.get("channel_id")
                if not channel_id:
                    continue

                try:
                    await refresh_channel_stats(
                        db, channel, youtube_service_manager, instagram_service_manager, growth_service
                    )
                except Exception as exc:
                    logger.error(f"Growth snapshot failed for channel '{channel_id}': {exc}")
                    await report_error(
                        feature="Growth tracking cron: per-channel",
                        message=f"Growth snapshot failed for '{channel_id}': {exc!s}",
                        exception=exc,
                        context={"channel_id": channel_id},
                    )

            metrics_service.track_task_end("growth_tracking", "success")
        except Exception as exc:
            logger.error(f"Growth tracking cron top-level error: {exc}")
            metrics_service.track_task_end("growth_tracking", "error")
            await report_error(
                feature="Growth tracking cron: top-level",
                message=f"Growth tracking cron cycle failed: {exc!s}",
                exception=exc,
            )

        interval = await _get_interval_seconds(db)
        logger.info(f"Growth tracking cron: sleeping {interval / 3600:.1f} hours until next run")
        await asyncio.sleep(interval)
