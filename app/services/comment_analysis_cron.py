from __future__ import annotations

"""Background cron loop for automated comment sentiment & demand analysis.

Runs once per day at a configurable hour (stored in the DB ``config``
collection, key ``comment_analysis_config``).  Defaults to 03:00 IST
if no config exists.

Follows the same ``asyncio.create_task`` pattern as ``auto_publisher.py``.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.database import update_channel_task_status
from app.services.comment_analysis_engine import run_cron_cycle
from app.services.gemini import GeminiService
from app.timezone import IST, now_ist

logger = get_logger(__name__)

_DEFAULT_HOUR = 3  # 03:00 IST
_INTERVAL_SECONDS = 86_400  # 24 hours


async def _get_analysis_hour(db: AsyncIOMotorDatabase) -> int:
    """Read the configured analysis hour from the ``config`` collection.

    Returns an int 0-23 (IST).  Falls back to ``_DEFAULT_HOUR`` if not set.
    """
    doc = await db.config.find_one({"key": "comment_analysis_config"})
    if doc:
        return int(doc.get("analysis_hour", _DEFAULT_HOUR))
    return _DEFAULT_HOUR


def _seconds_until_hour(target_hour: int) -> float:
    """Calculate seconds from now until the next occurrence of *target_hour* IST."""
    now = now_ist()
    target_today = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)

    if now >= target_today:
        target = target_today + timedelta(days=1)
    else:
        target = target_today

    return (target - now).total_seconds()


async def run_comment_analysis_cron(
    db: AsyncIOMotorDatabase,
    youtube_service_manager: Any,
    instagram_service_manager: Any,
    gemini_service: GeminiService,
) -> None:
    """Infinite loop — sleeps until the configured hour, runs analysis, repeats."""

    logger.info("Comment analysis cron started (default hour: %02d:00 IST)", _DEFAULT_HOUR)

    while True:
        target_hour = await _get_analysis_hour(db)
        wait_seconds = _seconds_until_hour(target_hour)

        next_run = now_ist() + timedelta(seconds=wait_seconds)
        logger.info(
            "⏰ Comment analysis cron: next run at %s IST (in %.0f min)",
            next_run.strftime("%Y-%m-%d %H:%M"),
            wait_seconds / 60,
        )

        await asyncio.sleep(wait_seconds)
        from app.services.metrics import metrics_service

        try:
            metrics_service.track_task_start("comment_analysis")
            channels = await db.channels.find().to_list(length=None)
            logger.info(
                "🔄 Comment analysis cron tick — processing %d channel(s)",
                len(channels),
            )

            for channel in channels:
                channel_id = channel.get("channel_id")
                platform = channel.get("platform", "youtube")
                if not channel_id:
                    continue

                try:
                    stats = await run_cron_cycle(
                        db=db,
                        youtube_service_manager=youtube_service_manager,
                        instagram_service_manager=instagram_service_manager,
                        gemini_service=gemini_service,
                        channel_id=channel_id,
                        platform=platform,
                    )
                    logger.info(
                        "✅ Comment analysis cron completed for '%s': %s",
                        channel_id, stats,
                    )
                    # Update channel status
                    await update_channel_task_status(db, channel_id, "comment_analysis")
                except Exception as exc:
                    logger.error(
                        "Comment analysis cron failed for channel '%s': %s",
                        channel_id, exc,
                    )
            metrics_service.track_task_end("comment_analysis", "success")
        except Exception as exc:
            logger.error("Comment analysis cron top-level error: %s", exc)
            metrics_service.track_task_end("comment_analysis", "error")
