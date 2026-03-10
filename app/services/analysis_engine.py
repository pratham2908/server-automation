from __future__ import annotations

"""Analysis engine – orchestrates the full analysis update flow.

Two-step pipeline:
  Step 1: Per-video analysis — each new video gets an individual Gemini
          analysis with stats snapshot, stored in ``analysis_history``.
  Step 2: Channel summary  — aggregates all per-video analyses into
          collective insights (best times, category scores, content param
          analysis, best combinations), stored in ``analysis``.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from app.timezone import now_ist

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.gemini import GeminiService
from app.services.todo_engine import update_categories_from_analysis
from app.services.youtube import YouTubeService

logger = get_logger(__name__)


async def run_analysis(
    channel_id: str,
    db: AsyncIOMotorDatabase,
    youtube_service: YouTubeService,
    gemini_service: GeminiService,
) -> dict[str, Any]:
    """Execute the full analysis pipeline for *channel_id*.

    Step 1 — Per-video analysis
    ---------------------------
    For each published video not yet in ``analysis_history``:
      a. Build a stats snapshot (YouTube metrics + subscriber data).
      b. Send to Gemini for individual AI analysis.
      c. Store the result in ``analysis_history`` (one doc per video).

    Step 2 — Channel summary
    ------------------------
    Aggregate all per-video analyses and send to Gemini to produce
    collective insights. Store in ``analysis`` (one doc per channel).
    Run the todo engine afterwards.
    """
    logger.info(
        "📥 Request received to analyze channel '%s'",
        channel_id,
        extra={"color": "CYAN"},
    )

    # ------------------------------------------------------------------
    # Fetch channel info (subscriber count) from YouTube
    # ------------------------------------------------------------------
    channel_doc = await db.channels.find_one({"channel_id": channel_id})
    if not channel_doc:
        return {}

    youtube_channel_id = channel_doc.get("youtube_channel_id", "")
    content_schema = channel_doc.get("content_schema", [])

    subscriber_count = 0
    try:
        channel_info = youtube_service.get_channel_info(youtube_channel_id)
        subscriber_count = channel_info.get("subscriber_count", 0)
    except Exception as exc:
        logger.warning("Could not fetch subscriber count: %s", exc)

    # ------------------------------------------------------------------
    # Step 1: Per-video analysis
    # ------------------------------------------------------------------
    done_videos = await db.videos.find(
        {"channel_id": channel_id, "status": "published"}
    ).to_list(length=None)

    already_analysed_ids: set[str] = set()
    async for doc in db.analysis_history.find(
        {"channel_id": channel_id}, {"video_id": 1}
    ):
        already_analysed_ids.add(doc["video_id"])

    new_videos = [
        v for v in done_videos if v["video_id"] not in already_analysed_ids
    ]

    logger.info(
        "📊 Found %d published videos, %d already analysed, %d new to analyse.",
        len(done_videos),
        len(already_analysed_ids),
        len(new_videos),
        extra={"color": "BLUE"},
    )

    # Exclude videos less than 3 days old.
    from app.timezone import IST
    three_days_ago = now_ist() - timedelta(days=3)

    filtered_videos = []
    for v in new_videos:
        v_created_at = v.get("created_at")
        if not v_created_at:
            filtered_videos.append(v)
            continue
        if v_created_at.tzinfo is None:
            v_created_at = v_created_at.replace(tzinfo=IST)
        if v_created_at <= three_days_ago:
            filtered_videos.append(v)

    new_videos = filtered_videos

    skipped = len(done_videos) - len(already_analysed_ids) - len(new_videos)
    if skipped > 0:
        logger.warning("⏳ 3-Day Filter: Skipped %d recent videos.", skipped)

    # Fetch YouTube stats + subscribers gained for new videos
    yt_ids = [v["youtube_video_id"] for v in new_videos if v.get("youtube_video_id")]
    yt_stats: dict[str, Any] = {}
    subs_gained: dict[str, int] = {}

    if yt_ids:
        logger.info(
            "📡 Fetching YouTube stats + subscribers gained for %d videos...",
            len(yt_ids),
            extra={"color": "CYAN"},
        )
        yt_stats = youtube_service.get_video_stats(yt_ids)
        subs_gained = youtube_service.get_subscribers_gained(yt_ids)

    # Analyze each new video individually
    per_video_count = 0
    for v in new_videos:
        yt_id = v.get("youtube_video_id")

        # Build stats snapshot
        stats: dict[str, Any] = {}
        if yt_id and yt_id in yt_stats:
            stats = dict(yt_stats[yt_id])
        else:
            meta = v.get("metadata") or {}
            stats = {
                k: meta[k]
                for k in (
                    "views", "likes", "comments", "duration_seconds",
                    "engagement_rate", "like_rate", "comment_rate",
                    "avg_percentage_viewed", "avg_view_duration_seconds",
                    "estimated_minutes_watched",
                )
                if meta.get(k) is not None
            }

        stats["subscribers_gained"] = subs_gained.get(yt_id, 0) if yt_id else 0
        stats["subscriber_count_at_analysis"] = subscriber_count
        views = stats.get("views", 0) or 0
        stats["views_per_subscriber"] = (
            round(views / subscriber_count, 4) if subscriber_count > 0 else 0
        )

        video_data_for_gemini = {
            "title": v.get("title", ""),
            "category": v.get("category", ""),
            "content_params": v.get("content_params") or {},
            "stats": stats,
        }

        # Call Gemini for per-video analysis
        try:
            ai_insight = await gemini_service.analyze_single_video(video_data_for_gemini)
        except Exception as exc:
            logger.warning("Gemini per-video analysis failed for '%s': %s", v.get("title", v["video_id"]), exc)
            ai_insight = {
                "performance_rating": 0,
                "what_worked": "Analysis failed",
                "what_didnt": str(exc),
                "key_learnings": [],
            }

        per_video_count += 1
        logger.info(
            "🔍 Analyzed [%d/%d] — \"%s\" (rating: %s)",
            per_video_count,
            len(new_videos),
            v.get("title", "Untitled")[:50],
            ai_insight.get("performance_rating", "?"),
            extra={"color": "MAGENTA"},
        )

        # Insert into analysis_history (one doc per video)
        await db.analysis_history.insert_one({
            "channel_id": channel_id,
            "video_id": v["video_id"],
            "youtube_video_id": yt_id,
            "title": v.get("title", ""),
            "category": v.get("category", ""),
            "content_params": v.get("content_params"),
            "stats_snapshot": stats,
            "ai_insight": ai_insight,
            "analyzed_at": now_ist(),
        })

    if per_video_count:
        logger.success(f"✅ Completed per-video analysis for {per_video_count} videos.")

    # ------------------------------------------------------------------
    # Step 2: Channel summary
    # ------------------------------------------------------------------
    all_per_video = await db.analysis_history.find(
        {"channel_id": channel_id}
    ).to_list(length=None)

    if not all_per_video:
        return {}

    # Build aggregated data for the channel summary prompt
    video_summaries: list[dict[str, Any]] = []
    all_analysed_ids: list[str] = []

    for pv in all_per_video:
        all_analysed_ids.append(pv["video_id"])
        video_summaries.append({
            "title": pv.get("title", ""),
            "category": pv.get("category", ""),
            "content_params": pv.get("content_params") or {},
            "stats": pv.get("stats_snapshot", {}),
            "ai_insight": pv.get("ai_insight", {}),
        })

    logger.info(
        "🧠 Running channel summary across %d per-video analyses...",
        len(video_summaries),
        extra={"color": "MAGENTA"},
    )

    # Fetch existing analysis for incremental refinement
    existing_analysis = await db.analysis.find_one({"channel_id": channel_id})

    BATCH_SIZE = 5
    running_analysis = (
        {
            "best_posting_times": existing_analysis.get("best_posting_times", []),
            "category_analysis": existing_analysis.get("category_analysis", []),
            "content_param_analysis": existing_analysis.get("content_param_analysis", []),
            "best_combinations": existing_analysis.get("best_combinations", []),
        }
        if existing_analysis
        else None
    )

    for i in range(0, len(video_summaries), BATCH_SIZE):
        batch = video_summaries[i : i + BATCH_SIZE]

        logger.info(
            "🧠 Channel Summary Batch (%d/%d)...",
            (i // BATCH_SIZE) + 1,
            (len(video_summaries) + BATCH_SIZE - 1) // BATCH_SIZE,
            extra={"color": "MAGENTA"},
        )

        running_analysis = await gemini_service.analyze_videos(
            batch, running_analysis, content_schema=content_schema or None,
        )

    updated = running_analysis or {}
    version = (existing_analysis.get("version", 0) + 1) if existing_analysis else 1

    analysis_doc = {
        "channel_id": channel_id,
        "subscriber_count": subscriber_count,
        "best_posting_times": updated.get("best_posting_times", []),
        "category_analysis": updated.get("category_analysis", []),
        "content_param_analysis": updated.get("content_param_analysis", []),
        "best_combinations": updated.get("best_combinations", []),
        "analysis_done_video_ids": all_analysed_ids,
        "version": version,
        "updated_at": now_ist(),
    }

    if existing_analysis:
        await db.analysis.update_one(
            {"channel_id": channel_id},
            {"$set": analysis_doc},
        )
    else:
        analysis_doc["created_at"] = now_ist()
        await db.analysis.insert_one(analysis_doc)

    logger.success(f"💾 Updated channel summary v{version} (subscriber_count={subscriber_count})")

    # Run category updates and archive underperformers
    await update_categories_from_analysis(
        channel_id=channel_id,
        analysis=analysis_doc,
        db=db,
        analysed_videos=new_videos,
    )

    # Return
    saved = await db.analysis.find_one({"channel_id": channel_id})
    if saved:
        saved.pop("_id", None)
        return saved

    analysis_doc.pop("_id", None)
    return analysis_doc
