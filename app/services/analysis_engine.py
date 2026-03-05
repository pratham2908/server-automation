from __future__ import annotations

"""Analysis engine – orchestrates the full analysis update flow.

Coordinates between the database, YouTube API, and Gemini AI to produce
an incremental channel analysis.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

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

    Flow
    ----
    1. Fetch all "published" videos from DB.
    2. Compute delta against already-analysed video IDs.
    3. If no new videos, return early.
    4. Fetch YouTube stats for new videos.
    5. Send data + previous analysis to Gemini.
    6. Save updated analysis to DB.
    7. Run the to-do list engine.
    8. Return the updated analysis document.
    """
    # 1  Fetch done videos
    done_videos = await db.videos.find(
        {"channel_id": channel_id, "status": "published"}
    ).to_list(length=None)
    
    logger.info(
        "📥 Request received to analyze channel '%s'",
        channel_id,
        extra={"color": "CYAN"},
    )

    # 2  Compute delta
    existing_analysis = await db.analysis.find_one({"channel_id": channel_id})
    already_analysed: set[str] = set()
    if existing_analysis:
        already_analysed = set(
            existing_analysis.get("analysis_done_video_ids", [])
        )

    new_videos = [
        v for v in done_videos if v["video_id"] not in already_analysed
    ]
    
    logger.info(
        "📊 Computing delta: Found %d 'done' videos total, %d already analysed, %d remaining.",
        len(done_videos),
        len(already_analysed),
        len(new_videos),
        extra={"color": "BLUE"},
    )

    # 2b  Exclude videos posted less than 3 days ago (not enough data yet).
    three_days_ago = datetime.utcnow() - timedelta(days=3)
    new_videos = [
        v for v in new_videos
        if v.get("created_at", datetime.min) <= three_days_ago
    ]
    
    skipped = len(done_videos) - len(already_analysed) - len(new_videos)
    if skipped > 0:
        logger.warning("⏳ 3-Day Filter: Skipped %d recent videos.", skipped)

    # 3  Early exit
    if not new_videos:
        return existing_analysis or {}

    # 4  Fetch YouTube stats for videos that have a youtube_video_id
    yt_ids = [
        v["youtube_video_id"]
        for v in new_videos
        if v.get("youtube_video_id")
    ]
    yt_stats: dict[str, Any] = {}
    if yt_ids:
        logger.info(
            "📡 Fetching updated YouTube stats for %d videos...",
            len(yt_ids),
            extra={"color": "CYAN"},
        )
        yt_stats = youtube_service.get_video_stats(yt_ids)

    # Enrich video data with stats
    video_data: list[dict[str, Any]] = []
    for v in new_videos:
        entry: dict[str, Any] = {
            "title": v.get("title", ""),
            "category": v.get("category", ""),
            "topic": v.get("topic", ""),
            "tags": v.get("tags", []),
        }
        yt_id = v.get("youtube_video_id")
        if yt_id and yt_id in yt_stats:
            entry["stats"] = yt_stats[yt_id]
        video_data.append(entry)

    # 5  Send to Gemini in batches of 5
    BATCH_SIZE = 5
    running_analysis = (
        {
            "best_posting_times": existing_analysis.get(
                "best_posting_times", []
            ),
            "category_analysis": existing_analysis.get(
                "category_analysis", []
            ),
        }
        if existing_analysis
        else None
    )

    for i in range(0, len(video_data), BATCH_SIZE):
        batch = video_data[i : i + BATCH_SIZE]
        
        logger.info(
            "🧠 Gemini Analysis Batch (%d/%d) in progress...",
            (i // BATCH_SIZE) + 1,
            (len(video_data) + BATCH_SIZE - 1) // BATCH_SIZE,
            extra={"color": "MAGENTA"},
        )

        running_analysis = await gemini_service.analyze_videos(
            batch, running_analysis
        )

    updated = running_analysis or {}

    # 6  Save to DB
    all_analysed = list(already_analysed | {v["video_id"] for v in new_videos})
    version = (existing_analysis.get("version", 0) + 1) if existing_analysis else 1

    analysis_doc = {
        "channel_id": channel_id,
        "best_posting_times": updated.get("best_posting_times", []),
        "category_analysis": updated.get("category_analysis", []),
        "analysis_done_video_ids": all_analysed,
        "version": version,
        "updated_at": datetime.utcnow(),
    }

    if existing_analysis:
        await db.analysis.update_one(
            {"channel_id": channel_id},
            {"$set": analysis_doc},
        )
    else:
        analysis_doc["created_at"] = datetime.utcnow()
        await db.analysis.insert_one(analysis_doc)
        
    logger.success(f"💾 Updated main analysis document v{version} in database")

    # 6b  Store audit snapshot in analysis_history
    history_doc = {
        "channel_id": channel_id,
        "version": version,
        "input_videos": video_data,
        "new_video_ids": [v["video_id"] for v in new_videos],
        "result": {
            "best_posting_times": updated.get("best_posting_times", []),
            "category_analysis": updated.get("category_analysis", []),
        },
        "total_analysed_count": len(all_analysed),
        "batch_count": (len(video_data) + BATCH_SIZE - 1) // BATCH_SIZE,
        "created_at": datetime.utcnow(),
    }
    await db.analysis_history.insert_one(history_doc)
    
    logger.success("🗃️ Saved audit trail snapshot to analysis_history")

    # 7  Run category updates and archive underperformers
    await update_categories_from_analysis(
        channel_id=channel_id, 
        analysis=analysis_doc, 
        db=db, 
        analysed_videos=new_videos,
    )

    # 8  Return
    saved = await db.analysis.find_one({"channel_id": channel_id})
    if saved:
        saved.pop("_id", None)
    return saved or analysis_doc
