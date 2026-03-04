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

from app.logger import get_logger
from app.services.gemini import GeminiService

logger = get_logger(__name__)

# Categories with a score below this **and** at least this many videos
# are candidates for archiving.
_ARCHIVE_SCORE_THRESHOLD = 30.0
_ARCHIVE_MIN_VIDEOS = 5


async def update_todo_list(
    channel_id: str,
    analysis: dict[str, Any],
    db: AsyncIOMotorDatabase,
    gemini_service: GeminiService,
    analysed_videos: list[dict[str, Any]] | None = None,
) -> None:
    """Refresh the to-do video list for *channel_id*.

    Steps
    -----
    1. Update category scores from Gemini's analysis output.
    2. Update category video_count for newly analysed videos.
    3. Archive categories whose score is below the threshold and that have
       enough videos to be statistically meaningful.
    4. For every active category in the analysis, generate a new to-do video
       (title / description / tags) via Gemini.
    5. Insert the new to-do videos into the ``videos`` collection.
    """
    # ------------------------------------------------------------------ #
    # 1  Update category scores from Gemini analysis output
    # ------------------------------------------------------------------ #
    logger.info("🔄 Updating category scores & video counts from new analysis...", extra={"color": "BLUE"})
    
    for cat_analysis in analysis.get("category_analysis", []):
        cat_name = cat_analysis.get("category", "")
        score = cat_analysis.get("score")
        if cat_name and score is not None:
            await db.categories.update_one(
                {"channel_id": channel_id, "name": cat_name},
                {
                    "$set": {
                        "score": score,
                        "updated_at": datetime.utcnow(),
                    }
                },
            )

    # ------------------------------------------------------------------ #
    # 2  Update video_count for newly analysed videos
    # ------------------------------------------------------------------ #
    if analysed_videos:
        # Count how many newly analysed videos belong to each category.
        cat_counts: dict[str, int] = {}
        for v in analysed_videos:
            cat = v.get("category", "")
            if cat:
                cat_counts[cat] = cat_counts.get(cat, 0) + 1

        for cat_name, count in cat_counts.items():
            await db.categories.update_one(
                {"channel_id": channel_id, "name": cat_name},
                {"$inc": {"video_count": count}},
            )

    # ------------------------------------------------------------------ #
    # 3  Archive underperforming categories
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
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            logger.warning(
                "📦 Archived underperforming category '%s' (score=%.1f)",
                cat_name,
                score,
            )

    # ------------------------------------------------------------------ #
    # 4  Generate new to-do videos — score-weighted distribution
    # ------------------------------------------------------------------ #
    #
    # Total to generate = number of newly analysed videos.
    # Slots are distributed across active categories proportionally to
    # their score, with every eligible category getting at least 1 slot
    # to keep the mix diverse.
    total_to_generate = len(analysed_videos) if analysed_videos else 0
    if total_to_generate == 0:
        logger.success("✅ Full Analysis & To-Do Engine Complete!", extra={"color": "BRIGHT_GREEN"})
        return
        
    logger.info("🧠 Generating new to-do video ideas (Target: %d videos)", total_to_generate, extra={"color": "MAGENTA"})

    active_categories = await db.categories.find(
        {"channel_id": channel_id, "status": "active"}
    ).to_list(length=None)

    # Build a lookup of category analysis by name for quick access.
    analysis_by_cat: dict[str, dict[str, Any]] = {
        ca["category"]: ca for ca in analysis.get("category_analysis", [])
    }

    # Only consider categories that have analysis insights.
    eligible = [
        c for c in active_categories if c["name"] in analysis_by_cat
    ]
    if not eligible:
        logger.success("✅ Full Analysis & To-Do Engine Complete!", extra={"color": "BRIGHT_GREEN"})
        return

    # Sort by score descending so highest-performing categories are first.
    eligible.sort(key=lambda c: c.get("score", 0), reverse=True)

    # Distribute slots: each eligible category gets at least 1, remaining
    # slots go to categories proportionally by score.
    slots: dict[str, int] = {}
    if total_to_generate <= len(eligible):
        # Fewer videos than categories — pick the top-scoring ones.
        for c in eligible[:total_to_generate]:
            slots[c["name"]] = 1
    else:
        # Give 1 to each, then distribute remaining by score weight.
        for c in eligible:
            slots[c["name"]] = 1

        remaining = total_to_generate - len(eligible)
        total_score = sum(c.get("score", 0) for c in eligible) or 1

        for c in eligible:
            share = int(remaining * (c.get("score", 0) / total_score))
            slots[c["name"]] += share

        # Distribute any leftover (from rounding) to top-scoring categories.
        distributed = sum(slots.values())
        leftover = total_to_generate - distributed
        for c in eligible[:leftover]:
            slots[c["name"]] += 1

    # Generate content for each slot.
    new_videos: list[dict[str, Any]] = []
    for cat_name, count in slots.items():
        cat_insights = analysis_by_cat[cat_name]
        for _ in range(count):
            try:
                content = await gemini_service.generate_video_content(
                    cat_name, cat_insights
                )
            except Exception:
                logger.exception(
                    "Failed to generate content for category '%s'", cat_name
                )
                continue
                
            logger.success(f"💡 Generated title: \"{content.get('title', 'Untitled')}\" (Category: {cat_name})")

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
    # 5  Bulk-insert new videos
    # ------------------------------------------------------------------ #
    if new_videos:
        await db.videos.insert_many(new_videos)
        logger.success(f"Inserted {len(new_videos)} new auto-generated To-Do videos into database")
        
    logger.success("✅ Full Analysis & To-Do Engine Complete!", extra={"color": "BRIGHT_GREEN"})

