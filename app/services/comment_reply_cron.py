from __future__ import annotations

"""Background cron loop for automated comment replies.

Runs every N hours (default 6, configurable via ``comment_reply_config``
in the ``config`` collection).  Iterates over all channels and calls
``run_comment_reply_cycle`` for each.

Follows the same ``asyncio.create_task`` pattern as ``auto_publisher.py``
and ``comment_analysis_cron.py``.
"""

import asyncio
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.comment_reply_engine import run_comment_reply_cycle
from app.services.gemini import GeminiService

logger = get_logger(__name__)

_DEFAULT_INTERVAL_HOURS = 6


async def _get_interval_seconds(db: AsyncIOMotorDatabase) -> float:
    doc = await db.config.find_one({"key": "comment_reply_config"})
    hours = _DEFAULT_INTERVAL_HOURS
    if doc:
        hours = doc.get("interval_hours", _DEFAULT_INTERVAL_HOURS)
    return float(hours) * 3600


async def run_comment_reply_cron(
    db: AsyncIOMotorDatabase,
    youtube_service_manager: Any,
    instagram_service_manager: Any,
    gemini_service: GeminiService,
) -> None:
    """Infinite loop — sleeps for the configured interval, runs reply cycles, repeats."""

    logger.info("Comment reply cron started (default interval: %dh)", _DEFAULT_INTERVAL_HOURS)

    while True:
        interval = await _get_interval_seconds(db)
        logger.info(
            "Comment reply cron: sleeping %.0f min until next run",
            interval / 60,
        )
        await asyncio.sleep(interval)
        from app.services.metrics import metrics_service

        try:
            metrics_service.track_task_start("comment_reply")
            channels = await db.channels.find().to_list(length=None)
            logger.info(
                "Comment reply cron tick — processing %d channel(s)",
                len(channels),
            )

            for channel in channels:
                channel_id = channel.get("channel_id")
                if not channel_id:
                    continue
                try:
                    stats = await run_comment_reply_cycle(
                        channel_id=channel_id,
                        db=db,
                        youtube_service_manager=youtube_service_manager,
                        instagram_service_manager=instagram_service_manager,
                        gemini_service=gemini_service,
                    )
                    logger.info(
                        "Comment reply cron completed for '%s': %s",
                        channel_id, stats,
                    )
                except Exception as exc:
                    logger.error(
                        "Comment reply cron failed for channel '%s': %s",
                        channel_id, exc,
                    )
            metrics_service.track_task_end("comment_reply", "success")
        except Exception as exc:
            logger.error("Comment reply cron top-level error: %s", exc)
            metrics_service.track_task_end("comment_reply", "error")
