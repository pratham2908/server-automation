from __future__ import annotations

"""To-do engine – archives underperforming categories and generates new
video ideas for the to-do list.

Called at the end of every analysis update to keep the to-do pipeline fresh.
"""

import logging
import uuid
from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.gemini import GeminiService

logger = logging.getLogger(__name__)

# Categories with a score below this **and** at least this many videos
# are candidates for archiving.
_ARCHIVE_SCORE_THRESHOLD = 30.0
_ARCHIVE_MIN_VIDEOS = 5


async def update_todo_list(
    channel_id: str,
    analysis: dict[str, Any],
    db: AsyncIOMotorDatabase,
    gemini_service: GeminiService,
) -> None:
    """Refresh the to-do video list for *channel_id*.

    Steps
    -----
    1. Archive categories whose score is below the threshold and that have
       enough videos to be statistically meaningful.
    2. For every active category in the analysis, generate a new to-do video
       (title / description / tags) via Gemini.
    3. Insert the new to-do videos into the ``videos`` collection.
    """
    # ------------------------------------------------------------------ #
    # 1  Archive underperforming categories
    # ------------------------------------------------------------------ #
    for cat_analysis in analysis.get("category_analysis", []):
        cat_name = cat_analysis.get("category", "")
        score = cat_analysis.get("score", 100)

        if score >= _ARCHIVE_SCORE_THRESHOLD:
            continue

        # Only archive if the category has enough videos to judge.
        cat_doc = await db.categories.find_one(
            {"channel_id": channel_id, "name": cat_name}
        )
        if cat_doc and cat_doc.get("video_count", 0) >= _ARCHIVE_MIN_VIDEOS:
            await db.categories.update_one(
                {"_id": cat_doc["_id"]},
                {
                    "$set": {
                        "status": "archived",
                        "score": score,
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            logger.info(
                "Archived category '%s' for channel %s (score=%.1f)",
                cat_name,
                channel_id,
                score,
            )

    # ------------------------------------------------------------------ #
    # 2  Generate new to-do videos for active categories
    # ------------------------------------------------------------------ #
    active_categories = await db.categories.find(
        {"channel_id": channel_id, "status": "active"}
    ).to_list(length=None)

    # Build a lookup of category analysis by name for quick access.
    analysis_by_cat: dict[str, dict[str, Any]] = {
        ca["category"]: ca for ca in analysis.get("category_analysis", [])
    }

    new_videos: list[dict[str, Any]] = []
    for cat_doc in active_categories:
        cat_name = cat_doc["name"]
        cat_insights = analysis_by_cat.get(cat_name, {})

        if not cat_insights:
            continue

        try:
            content = await gemini_service.generate_video_content(
                cat_name, cat_insights
            )
        except Exception:
            logger.exception(
                "Failed to generate content for category '%s'", cat_name
            )
            continue

        video_doc = {
            "channel_id": channel_id,
            "video_id": str(uuid.uuid4()),
            "title": content.get("title", ""),
            "description": content.get("description", ""),
            "tags": content.get("tags", []),
            "category": cat_name,
            "topic": "",
            "status": "todo",
            "suggested": False,
            "basis_factor": f"Auto-generated from analysis v{analysis.get('version', '?')}",
            "youtube_video_id": None,
            "r2_object_key": None,
            "metadata": {
                "views": None,
                "engagement": None,
                "avg_percentage_viewed": None,
            },
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        new_videos.append(video_doc)

    # ------------------------------------------------------------------ #
    # 3  Bulk-insert new videos
    # ------------------------------------------------------------------ #
    if new_videos:
        await db.videos.insert_many(new_videos)
        logger.info(
            "Inserted %d new to-do videos for channel %s",
            len(new_videos),
            channel_id,
        )
