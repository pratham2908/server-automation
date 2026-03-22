from __future__ import annotations

"""To-do engine – archives underperforming categories and generates new
video ideas for the to-do list.

Called at the end of every analysis update to keep the to-do pipeline fresh.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from app.timezone import IST, UTC, now_ist

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.gemini import GeminiService

logger = get_logger(__name__)

# Categories with a score below this **and** at least this many videos
# are candidates for archiving.
_ARCHIVE_SCORE_THRESHOLD = 30.0
_ARCHIVE_MIN_VIDEOS = 5


async def _compute_category_metadata(
    channel_id: str,
    category_name: str,
    db: AsyncIOMotorDatabase,
) -> dict[str, Any]:
    """Aggregate performance metrics for published videos in a category that
    have been analyzed (exist in analysis_history).
    """
    # Only include videos whose analysis has been completed
    analysed_video_ids: set[str] = set()
    async for doc in db.analysis_history.find(
        {"channel_id": channel_id},
        {"video_id": 1},
    ):
        analysed_video_ids.add(doc["video_id"])

    all_published = await db.videos.find(
        {
            "channel_id": channel_id,
            "category": category_name,
            "status": "published",
        }
    ).to_list(length=None)

    videos = [v for v in all_published if v["video_id"] in analysed_video_ids]

    if not videos:
        return {"total_videos": 0, "video_ids": []}

    def _avg(key: str) -> float | None:
        vals = [
            v["metadata"][key]
            for v in videos
            if v.get("metadata") and v["metadata"].get(key) is not None
        ]
        return round(sum(vals) / len(vals), 2) if vals else None

    total_views_vals = [
        v["metadata"]["views"]
        for v in videos
        if v.get("metadata") and v["metadata"].get("views") is not None
    ]
    total_emw_vals = [
        v["metadata"]["estimated_minutes_watched"]
        for v in videos
        if v.get("metadata") and v["metadata"].get("estimated_minutes_watched") is not None
    ]

    eligible_video_ids = [v["video_id"] for v in videos]

    return {
        "total_videos": len(videos),
        "video_ids": eligible_video_ids,
        "avg_views": _avg("views"),
        "avg_likes": _avg("likes"),
        "avg_comments": _avg("comments"),
        "avg_duration_seconds": _avg("duration_seconds"),
        "avg_engagement_rate": _avg("engagement_rate"),
        "avg_like_rate": _avg("like_rate"),
        "avg_comment_rate": _avg("comment_rate"),
        "avg_percentage_viewed": _avg("avg_percentage_viewed"),
        "avg_view_duration_seconds": _avg("avg_view_duration_seconds"),
        "total_views": sum(total_views_vals) if total_views_vals else None,
        "total_estimated_minutes_watched": (
            round(sum(total_emw_vals), 1) if total_emw_vals else None
        ),
        "avg_subscribers": _avg("subscribers_gained"),
        "avg_shares": _avg("shares"),
        "avg_saves": _avg("saves"),
        "avg_reach": _avg("reach"),
    }


async def recompute_category(
    channel_id: str,
    category_name: str,
    db: AsyncIOMotorDatabase,
) -> None:
    """Recompute and persist metadata, video_count, and video_ids for one category."""
    meta = await _compute_category_metadata(channel_id, category_name, db)
    await db.categories.update_one(
        {"channel_id": channel_id, "name": category_name},
        {
            "$set": {
                "metadata": meta,
                "video_count": meta.get("total_videos", 0),
                "video_ids": meta.get("video_ids", []),
                "updated_at": now_ist(),
            }
        },
    )


async def update_categories_from_analysis(
    channel_id: str,
    analysis: dict[str, Any],
    db: AsyncIOMotorDatabase,
    analysed_videos: list[dict[str, Any]] | None = None,
) -> None:
    """Update category scores, metadata (and video_count from it), and archive underperformers."""
    logger.info("🔄 Updating category scores, metadata & video_count from new analysis...", extra={"color": "BLUE"})

    # 1. Update scores
    for cat_analysis in analysis.get("category_analysis", []):
        cat_name = cat_analysis.get("category", "")
        score = cat_analysis.get("score")
        if cat_name and score is not None:
            await db.categories.update_one(
                {"channel_id": channel_id, "name": cat_name},
                {
                    "$set": {
                        "score": score,
                        "updated_at": now_ist(),
                    }
                },
            )

    # 2. Compute and persist aggregated metadata per category (eligible-for-analysis videos only),
    #    and set video_count from that same count
    all_categories = await db.categories.find(
        {"channel_id": channel_id}
    ).to_list(length=None)

    for cat_doc in all_categories:
        cat_name = cat_doc["name"]
        meta = await _compute_category_metadata(channel_id, cat_name, db)
        await db.categories.update_one(
            {"channel_id": channel_id, "name": cat_name},
            {
                "$set": {
                    "metadata": meta,
                    "video_count": meta.get("total_videos", 0),
                    "video_ids": meta.get("video_ids", []),
                    "updated_at": now_ist(),
                }
            },
        )
    logger.success("📊 Computed and saved metadata for %d categories", len(all_categories))

    # 3. Archive underperformers
    for cat_analysis in analysis.get("category_analysis", []):
        cat_name = cat_analysis.get("category", "")
        score = cat_analysis.get("score", 100)

        if score >= _ARCHIVE_SCORE_THRESHOLD:
            continue

        cat_doc = await db.categories.find_one(
            {"channel_id": channel_id, "name": cat_name}
        )
        if cat_doc and cat_doc.get("video_count", 0) >= _ARCHIVE_MIN_VIDEOS:
            await db.categories.update_one(
                {"channel_id": channel_id, "name": cat_name},
                {
                    "$set": {
                        "status": "archived",
                        "updated_at": now_ist(),
                    }
                },
            )
            logger.warning(
                "📦 Archived underperforming category '%s' (score=%.1f)",
                cat_name,
                score,
            )

    logger.success("✅ Category updates complete", extra={"color": "BRIGHT_GREEN"})


async def generate_todo_videos(
    channel_id: str,
    target_count: int,
    db: AsyncIOMotorDatabase,
    gemini_service: GeminiService,
) -> None:
    """Generate 'target_count' new to-do videos for active categories.

    Steps
    -----
    1. Delete any existing "todo" status videos that belong to archived categories.
    2. Distribute `target_count` slots across active categories weighted by score.
    3. Exclude existing titles.
    4. Call Gemini to bulk-generate distinct ideas per category.
    """
    if target_count <= 0:
        return

    # ------------------------------------------------------------------ #
    # 1. Clean up archived categories' todo videos
    # ------------------------------------------------------------------ #
    archived_categories = await db.categories.find(
        {"channel_id": channel_id, "status": "archived"}
    ).to_list(length=None)
    archived_names = [c["name"] for c in archived_categories]

    if archived_names:
        delete_result = await db.videos.delete_many(
            {
                "channel_id": channel_id,
                "status": "todo",
                "category": {"$in": archived_names},
            }
        )
        if delete_result.deleted_count > 0:
            logger.warning(
                "🗑️ Deleted %d pending todo videos from archived categories",
                delete_result.deleted_count
            )

    # ------------------------------------------------------------------ #
    # 2. Slot distribution
    # ------------------------------------------------------------------ #
    active_categories = await db.categories.find(
        {"channel_id": channel_id, "status": "active"}
    ).to_list(length=None)

    from app.database import get_content_schema_for_prompt

    channel_doc = await db.channels.find_one({"channel_id": channel_id})
    platform = (channel_doc or {}).get("platform", "youtube")

    latest_analysis = await db.analysis.find_one(
        {"channel_id": channel_id}, sort=[("version", -1)]
    ) or {}
    
    analysis_by_cat: dict[str, dict[str, Any]] = {
        ca["category"]: ca for ca in latest_analysis.get("category_analysis", [])
    }
    content_param_analysis = latest_analysis.get("content_param_analysis", [])
    best_combinations = latest_analysis.get("best_combinations", [])

    eligible = [
        c for c in active_categories if c["name"] in analysis_by_cat
    ]
    if not eligible:
        logger.warning("No eligible categories with analysis insights found.")
        return

    eligible.sort(key=lambda c: c.get("score", 0), reverse=True)

    slots: dict[str, int] = {}
    if target_count <= len(eligible):
        # Fewer videos than categories — pick the top-scoring ones.
        for c in eligible[:target_count]:
            slots[c["name"]] = 1
    else:
        # Give 1 to each, then distribute remaining by score weight.
        for c in eligible:
            slots[c["name"]] = 1

        remaining = target_count - len(eligible)
        total_score = sum(c.get("score", 0) for c in eligible) or 1

        for c in eligible:
            share = int(remaining * (c.get("score", 0) / total_score))
            slots[c["name"]] += share

        distributed = sum(slots.values())
        leftover = target_count - distributed
        for c in eligible[:leftover]:
            slots[c["name"]] += 1

    # ------------------------------------------------------------------ #
    # 3. Generate content
    # ------------------------------------------------------------------ #
    total_slots = sum(slots.values())
    logger.info("🧠 Generating %d new to-do video ideas", total_slots, extra={"color": "MAGENTA"})
    new_videos: list[dict[str, Any]] = []
    
    global_idx = 0

    for cat_name, count in slots.items():
        if count == 0:
            continue
            
        cat_insights = analysis_by_cat[cat_name]

        content_schema = await get_content_schema_for_prompt(db, channel_id, category=cat_name)

        # Fetch existing titles AND content_params to avoid duplication
        existing_docs = await db.videos.find(
            {"channel_id": channel_id, "category": cat_name},
            {"title": 1, "content_params": 1},
        ).to_list(length=None)
        existing_titles = [doc.get("title", "") for doc in existing_docs if doc.get("title")]
        existing_content_params = [
            doc["content_params"] for doc in existing_docs
            if doc.get("content_params")
        ]

        try:
            generated_list = await gemini_service.generate_video_content(
                channel_id=channel_id,
                category=cat_name,
                category_analysis=cat_insights,
                count=count,
                existing_titles=existing_titles,
                content_schema=content_schema or None,
                content_param_analysis=content_param_analysis or None,
                best_combinations=best_combinations or None,
                existing_content_params=existing_content_params or None,
                platform=platform,
            )
        except Exception:
            logger.exception("Failed to generate content for category '%s'", cat_name)
            continue

        for content in generated_list:
            global_idx += 1

            logger.success(f"💡 Generated [{global_idx}/{total_slots}] - \"{content.get('title', 'Untitled')}\" (Category: {cat_name})")

            gen_params = content.get("content_params") or {}

            video_doc = {
                "channel_id": channel_id,
                "video_id": str(uuid.uuid4()),
                "title": content.get("title", ""),
                "description": content.get("description", ""),
                "tags": content.get("tags", []),
                "category": cat_name,
                "status": "todo",
                "suggested": False,
                "youtube_video_id": None,
                "instagram_media_id": None,
                "r2_object_key": None,
                "metadata": {
                    "views": None,
                    "engagement": None,
                    "avg_percentage_viewed": None,
                },
                "content_params": gen_params,
                "verification_status": "verified",
                "created_at": now_ist(),
                "updated_at": now_ist(),
            }
            new_videos.append(video_doc)

    # ------------------------------------------------------------------ #
    # 4. Insert
    # ------------------------------------------------------------------ #
    if new_videos:
        await db.videos.insert_many(new_videos)
        logger.success(f"Inserted {len(new_videos)} new auto-generated To-Do videos into database")
        
    logger.success("✅ To-Do Generation Complete!", extra={"color": "BRIGHT_GREEN"})

