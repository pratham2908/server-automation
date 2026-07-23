"""To-do engine – archives underperforming categories and generates new
video ideas for the to-do list.

Called at the end of every analysis update to keep the to-do pipeline fresh.
"""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.timezone import now_ist

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
    videos = await db.videos.find(
        {
            "channel_id": channel_id,
            "category": category_name,
            "status": "published",
            "performance": {"$ne": None},
        }
    ).to_list(length=None)

    if not videos:
        return {"total_videos": 0, "video_ids": []}

    def _avg(key: str) -> float | None:
        vals = [v["metadata"][key] for v in videos if v.get("metadata") and v["metadata"].get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    total_views_vals = [
        v["metadata"]["views"] for v in videos if v.get("metadata") and v["metadata"].get("views") is not None
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
        "total_estimated_minutes_watched": (round(sum(total_emw_vals), 1) if total_emw_vals else None),
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
    logger.info(
        "🔄 Updating category scores, metadata & video_count from new analysis...",
        extra={"color": "BLUE"},
    )

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
    all_categories = await db.categories.find({"channel_id": channel_id}).to_list(length=None)

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

        cat_doc = await db.categories.find_one({"channel_id": channel_id, "name": cat_name})
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


