"""Background cron loop for per-channel auto-sync + analysis.

Only channels that opt in via ``automation_config.auto_sync.enabled`` are
synced: every 15 minutes while they have a video in its first week and daily
otherwise, on fixed clock slots (see ``auto_sync_schedule``). A tick every minute
picks the channels whose ``last_tasks.auto_sync`` is older than their newest slot,
and for each one:

1. Refreshes the channel's numbers (subscribers, views, profile).
2. Calls the existing video sync logic (YouTube or Instagram).
3. Counts newly-eligible unanalyzed videos.
4. If the count meets the threshold, triggers a full analysis via ``run_analysis``.

The ``sync_analysis_config`` document in the ``config`` collection holds the
global kill switch (``enabled``) and the ``analysis_threshold``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_channel_platform, not_paused_query, update_channel_task_status
from app.exceptions import ChannelNotConnectedError
from app.logger import get_logger
from app.services.analysis_engine import ANALYSIS_MIN_AGE_DAYS, run_analysis
from app.services.auto_sync_schedule import AUTO_SYNC_TASK, FRESH_REEL_DAYS, is_auto_sync_due, seconds_to_next_minute
from app.services.channel_stats import refresh_channel_stats
from app.services.error_reporting import report_error
from app.services.gemini import GeminiService
from app.services.growth_tracking import GrowthTrackingService
from app.services.metrics import metrics_service
from app.services.video_service import VideoService
from app.timezone import now_ist

logger = get_logger(__name__)

_DEFAULT_ANALYSIS_THRESHOLD = 3
# How often the cron looks for due channels. It wakes on the minute, so a sync
# slot (:05, :20, …) starts within a second of its time; the tick itself is a
# single indexed query.
_TICK_SECONDS = 60


async def _get_config(db: AsyncIOMotorDatabase) -> dict:
    """Read the sync-analysis pipeline config from the ``config`` collection."""
    doc = await db.config.find_one({"key": "sync_analysis_config"})
    return doc or {}


async def _count_unanalyzed_videos(
    db: AsyncIOMotorDatabase,
    channel_id: str,
) -> int:
    """Count published videos the analysis would actually pick up.

    Matches the engine's 3-day filter (on ``created_at``): counting videos it
    skips kept a channel at the threshold after every run, so each hourly sync
    re-ran the analysis and its paid AI calls with nothing new to analyse.
    """
    cutoff = now_ist() - timedelta(days=ANALYSIS_MIN_AGE_DAYS)
    count = await db.videos.count_documents(
        {
            "channel_id": channel_id,
            "status": "published",
            "performance": None,
            "$or": [{"created_at": {"$lte": cutoff}}, {"created_at": None}],
        }
    )
    return count


async def _channels_with_fresh_reels(db: AsyncIOMotorDatabase, now: datetime) -> set[str]:
    """Channels with a video published in the last week — those sync every 15 minutes."""
    since = now - timedelta(days=FRESH_REEL_DAYS)
    return set(await db.videos.distinct("channel_id", {"status": "published", "published_at": {"$gte": since}}))


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
    import app.main as main_mod

    assert main_mod.r2_service is not None
    service = VideoService(
        db=db,
        r2_service=main_mod.r2_service,
        gemini_service=gemini_service,
        youtube_manager=youtube_service_manager,
        instagram_manager=instagram_service_manager,
    )

    platform = get_channel_platform(channel)
    result: dict[str, Any] = {
        "channel_id": channel_id,
        "platform": platform,
        "stats": None,
        "sync": None,
        "analysis": None,
    }

    # --- Channel numbers ---
    # Independent of the video sync: a failure here is reported but must not
    # stop the videos from syncing, and vice versa.
    try:
        refreshed = await refresh_channel_stats(
            db, channel, youtube_service_manager, instagram_service_manager, GrowthTrackingService(db)
        )
        result["stats"] = "ok" if refreshed else "skipped (no credential)"
    except ChannelNotConnectedError as exc:
        result["stats"] = f"skipped ({exc})"
    except Exception as exc:
        result["stats"] = f"error ({exc})"
        logger.error("Auto-sync stats refresh failed for '%s': %s", channel_id, exc)
        await report_error(
            feature="Sync-analysis cron: channel stats",
            message=f"Channel stats refresh failed for '{channel_id}': {exc!s}",
            exception=exc,
            context={"channel_id": channel_id},
        )

    # --- Sync ---
    try:
        # Incremental: a 15-minute sync re-reads only fresh reels every time and older
        # ones once a day, so its cost doesn't grow with the back catalogue.
        await service.sync_videos(channel_id=channel_id, instructions=None, incremental=True)
        result["sync"] = "ok"
        logger.info(
            "Auto-sync completed for '%s' (%s)",
            channel_id,
            platform,
        )
    except HTTPException as exc:
        result["sync"] = f"skipped ({exc.detail})"
        logger.warning(
            "Auto-sync skipped for '%s': %s",
            channel_id,
            exc.detail,
        )
        return result
    except ChannelNotConnectedError as exc:
        # Not a fault — the channel needs reconnecting in the UI. Reporting it
        # would file an identical error-queue entry on every cron run.
        result["sync"] = f"skipped ({exc})"
        logger.warning(
            "Auto-sync skipped for '%s': %s — reconnect the channel to resume",
            channel_id,
            exc,
        )
        return result
    except Exception as exc:
        result["sync"] = f"error ({exc})"
        logger.error("Auto-sync failed for '%s': %s", channel_id, exc)
        await report_error(
            feature="Sync-analysis cron: channel sync",
            message=f"Auto-sync failed for '{channel_id}': {exc!s}",
            exception=exc,
            context={"channel_id": channel_id},
        )
        return result

    if platform == "threads":
        # Threads posts are packaged from their linked channel's analysis; there is no
        # Threads-side video analysis to run.
        await update_channel_task_status(db, channel_id, "sync_analysis")
        return result

    # --- Check unanalyzed count ---
    unanalyzed = await _count_unanalyzed_videos(db, channel_id)
    result["unanalyzed_count"] = unanalyzed

    if unanalyzed < analysis_threshold:
        logger.info(
            "Channel '%s': %d unanalyzed video(s) < threshold %d — skipping analysis",
            channel_id,
            unanalyzed,
            analysis_threshold,
        )
        await update_channel_task_status(db, channel_id, "sync_analysis")
        return result

    # --- Analysis ---
    try:
        youtube_service = None
        instagram_service = None

        if platform == "youtube" and youtube_service_manager:
            youtube_service = await youtube_service_manager.get_service(channel_id)
        if platform == "instagram" and instagram_service_manager:
            instagram_service = await instagram_service_manager.get_service(channel_id)

        await run_analysis(
            channel_id,
            db,
            youtube_service,
            gemini_service,
            instagram_service=instagram_service,
            platform=platform,
        )
        result["analysis"] = "ok"
        await update_channel_task_status(db, channel_id, "video_sync")
        logger.info(
            "Auto-analysis completed for '%s' (%d unanalyzed videos triggered it)",
            channel_id,
            unanalyzed,
        )
    except Exception as exc:
        result["analysis"] = f"error ({exc})"
        logger.error("Auto-analysis failed for '%s': %s", channel_id, exc)
        await report_error(
            feature="Sync-analysis cron: channel analysis",
            message=f"Auto-analysis failed for '{channel_id}': {exc!s}",
            exception=exc,
            context={"channel_id": channel_id},
        )

    await update_channel_task_status(db, channel_id, "sync_analysis")
    return result


async def run_sync_analysis_cron(
    db: AsyncIOMotorDatabase,
    youtube_service_manager: Any,
    instagram_service_manager: Any,
    gemini_service: GeminiService,
) -> None:
    """Infinite loop — every tick, sync + analyse each opted-in channel that is due."""

    logger.info("Auto-sync cron started (tick: %ds, per-channel intervals)", _TICK_SECONDS)

    while True:
        await asyncio.sleep(seconds_to_next_minute(now_ist()))

        try:
            config = await _get_config(db)
            if not config.get("enabled", True):
                continue

            now = now_ist()
            candidates = await db.channels.find(
                {"automation_config.auto_sync.enabled": True, **not_paused_query()}
            ).to_list(length=None)
            fresh = await _channels_with_fresh_reels(db, now)
            due = [
                c
                for c in candidates
                if c.get("channel_id") and is_auto_sync_due(c, now, has_fresh_reel=c["channel_id"] in fresh)
            ]
            if not due:
                continue

            metrics_service.track_task_start("sync_analysis")
            threshold = config.get("analysis_threshold", _DEFAULT_ANALYSIS_THRESHOLD)
            logger.info("Auto-sync cron tick — %d channel(s) due", len(due))

            results = []
            for channel in due:
                channel_id = channel["channel_id"]
                # Stamp before running, not after: a sync that fails (an IG rate
                # limit, an expired token) then waits a full interval instead of
                # being retried — and re-failing — on every tick.
                await update_channel_task_status(db, channel_id, AUTO_SYNC_TASK)
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
                        "Auto-sync cron failed for channel '%s': %s",
                        channel_id,
                        exc,
                    )
                    await report_error(
                        feature="Sync-analysis cron: per-channel",
                        message=f"Sync-analysis failed for '{channel_id}': {exc!s}",
                        exception=exc,
                        context={"channel_id": channel_id},
                    )

            metrics_service.track_task_end("sync_analysis", "success")
            logger.info("Auto-sync cron cycle complete: %s", results)
        except Exception as exc:
            logger.error("Auto-sync cron top-level error: %s", exc)
            metrics_service.track_task_end("sync_analysis", "error")
            await report_error(
                feature="Sync-analysis cron: top-level",
                message=f"Sync-analysis cron cycle failed: {exc!s}",
                exception=exc,
            )
