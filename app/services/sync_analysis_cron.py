from __future__ import annotations

"""Background cron loop for automated video sync + analysis.

Runs every N hours (default 12, configurable via ``sync_analysis_config``
in the ``config`` collection).  For each channel it:

1. Calls the existing video sync logic (YouTube or Instagram).
2. Counts newly-eligible unanalyzed videos.
3. If the count meets the configurable threshold, triggers a full
   analysis update via ``run_analysis``.

Follows the same ``asyncio.create_task`` pattern as the other crons.
"""

import asyncio
from typing import Any

from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.analysis_engine import run_analysis
from app.database import update_channel_task_status
from app.services.gemini import GeminiService

logger = get_logger(__name__)

_DEFAULT_INTERVAL_HOURS = 12
_DEFAULT_ANALYSIS_THRESHOLD = 3


async def _get_config(db: AsyncIOMotorDatabase) -> dict:
    """Read the sync-analysis pipeline config from the ``config`` collection."""
    doc = await db.config.find_one({"key": "sync_analysis_config"})
    return doc or {}


async def _count_unanalyzed_videos(
    db: AsyncIOMotorDatabase, channel_id: str,
) -> int:
    """Count published + verified videos that have no ``analysis_history`` entry."""
    analyzed_ids = set()
    async for doc in db.analysis_history.find(
        {"channel_id": channel_id}, {"video_id": 1},
    ):
        analyzed_ids.add(doc["video_id"])

    count = 0
    async for doc in db.videos.find(
        {
            "channel_id": channel_id,
            "status": "published",
            "verification_status": {"$ne": "unverified"},
        },
        {"video_id": 1},
    ):
        if doc["video_id"] not in analyzed_ids:
            count += 1

    return count


async def run_sync_analysis_for_channel(
    channel_id: str,
    channel: dict,
    db: AsyncIOMotorDatabase,
    youtube_service_manager: Any,
    instagram_service_manager: Any,
    gemini_service: GeminiService,
    analysis_threshold: int = _DEFAULT_ANALYSIS_THRESHOLD,
) -> dict[str, Any]:
    """Run sync + conditional analysis for a single channel.

    Returns a summary dict with sync and analysis results.
    """
    from app.routers.videos import sync_videos

    platform = channel.get("platform", "youtube")
    result: dict[str, Any] = {
        "channel_id": channel_id,
        "platform": platform,
        "sync": None,
        "analysis": None,
    }

    # --- Sync ---
    try:
        sync_result = await sync_videos(
            channel_id=channel_id, body=None, db=db,
        )
        result["sync"] = "ok"
        logger.info(
            "Auto-sync completed for '%s' (%s)",
            channel_id, platform,
        )
    except HTTPException as exc:
        result["sync"] = f"skipped ({exc.detail})"
        logger.warning(
            "Auto-sync skipped for '%s': %s", channel_id, exc.detail,
        )
        return result
    except Exception as exc:
        result["sync"] = f"error ({exc})"
        logger.error("Auto-sync failed for '%s': %s", channel_id, exc)
        return result

    # --- Check unanalyzed count ---
    unanalyzed = await _count_unanalyzed_videos(db, channel_id)
    result["unanalyzed_count"] = unanalyzed

    if unanalyzed < analysis_threshold:
        logger.info(
            "Channel '%s': %d unanalyzed video(s) < threshold %d — skipping analysis",
            channel_id, unanalyzed, analysis_threshold,
        )
        return result

    # --- Analysis ---
    try:
        youtube_service = None
        instagram_service = None

        if platform == "youtube" and youtube_service_manager:
            youtube_service = await youtube_service_manager.get_service(channel_id)
        if platform == "instagram" and instagram_service_manager:
            instagram_service = await instagram_service_manager.get_service(channel_id)

        analysis_result = await run_analysis(
            channel_id, db, youtube_service, gemini_service,
            instagram_service=instagram_service,
            platform=platform,
        )
        result["analysis"] = "ok"
        logger.info(
            "Auto-analysis completed for '%s' (%d unanalyzed videos triggered it)",
            channel_id, unanalyzed,
        )
    except Exception as exc:
        result["analysis"] = f"error ({exc})"
        logger.error("Auto-analysis failed for '%s': %s", channel_id, exc)

    await update_channel_task_status(db, channel_id, "sync_analysis")
    return result


async def run_sync_analysis_cron(
    db: AsyncIOMotorDatabase,
    youtube_service_manager: Any,
    instagram_service_manager: Any,
    gemini_service: GeminiService,
) -> None:
    """Infinite loop — sleeps for the configured interval, runs sync + analysis, repeats."""

    logger.info(
        "Sync-analysis cron started (default interval: %dh, threshold: %d)",
        _DEFAULT_INTERVAL_HOURS, _DEFAULT_ANALYSIS_THRESHOLD,
    )

    while True:
        config = await _get_config(db)
        interval_hours = config.get("interval_hours", _DEFAULT_INTERVAL_HOURS)
        interval_seconds = float(interval_hours) * 3600
        enabled = config.get("enabled", True)

        logger.info(
            "Sync-analysis cron: sleeping %.0f min until next run (enabled=%s)",
            interval_seconds / 60, enabled,
        )
        await asyncio.sleep(interval_seconds)

        if not enabled:
            logger.info("Sync-analysis cron: disabled via config — skipping this cycle")
            continue

        from app.services.metrics import metrics_service

        try:
            metrics_service.track_task_start("sync_analysis")
            config = await _get_config(db)
            threshold = config.get("analysis_threshold", _DEFAULT_ANALYSIS_THRESHOLD)

            channels = await db.channels.find().to_list(length=None)
            logger.info(
                "Sync-analysis cron tick — processing %d channel(s)",
                len(channels),
            )

            results = []
            for channel in channels:
                channel_id = channel.get("channel_id")
                if not channel_id:
                    continue
                try:
                    r = await run_sync_analysis_for_channel(
                        channel_id=channel_id,
                        channel=channel,
                        db=db,
                        youtube_service_manager=youtube_service_manager,
                        instagram_service_manager=instagram_service_manager,
                        gemini_service=gemini_service,
                        analysis_threshold=threshold,
                    )
                    results.append(r)
                except Exception as exc:
                    logger.error(
                        "Sync-analysis cron failed for channel '%s': %s",
                        channel_id, exc,
                    )

            metrics_service.track_task_end("sync_analysis", "success")
            logger.info("Sync-analysis cron cycle complete: %s", results)
        except Exception as exc:
            logger.error("Sync-analysis cron top-level error: %s", exc)
            metrics_service.track_task_end("sync_analysis", "error")
