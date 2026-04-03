"""Retention analysis service — orchestrates video-file analysis via Gemini.

Downloads the video from R2, uploads to Gemini for multimodal analysis,
stores the structured retention prediction, and cleans up temp files.
Also provides a helper for computing predicted-vs-actual deviation.
"""

from __future__ import annotations

import os
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.gemini import GeminiService
from app.services.pacing_templates import PacingTemplateService
from app.services.r2 import R2Service
from app.timezone import now_ist

logger = get_logger(__name__)


async def run_retention_analysis(
    channel_id: str,
    video_id: str,
    db: AsyncIOMotorDatabase,
    r2_service: R2Service,
    gemini_service: GeminiService,
) -> None:
    """Analyze a video's retention potential via Gemini multimodal.

    1. Look up the video doc for R2 key, title, and platform.
    2. Mark the ``retention_analysis`` doc as ``analyzing``.
    3. Download from R2 to a temp file.
    4. Send to Gemini for video retention analysis.
    5. Store result (``completed`` or ``failed``).
    6. Clean up the temp file.
    """
    video = await db.videos.find_one(
        {"channel_id": channel_id, "video_id": video_id}
    )
    if not video:
        logger.error("Retention analysis: video %s not found", video_id)
        return

    r2_key = video.get("r2_object_key")
    if not r2_key:
        logger.error("Retention analysis: video %s has no R2 key", video_id)
        return

    video_title = video.get("title", "")
    channel_doc = await db.channels.find_one({"channel_id": channel_id})
    platform = (channel_doc or {}).get("platform", "youtube")

    now = now_ist()
    await db.retention_analysis.update_one(
        {"channel_id": channel_id, "video_id": video_id},
        {
            "$set": {
                "status": "analyzing",
                "video_title": video_title,
                "platform": platform,
                "error_message": None,
                "updated_at": now,
            },
            "$setOnInsert": {
                "channel_id": channel_id,
                "video_id": video_id,
                "created_at": now,
            },
        },
        upsert=True,
    )

    temp_path: str | None = None
    try:
        logger.info(
            "Downloading video '%s' from R2 for retention analysis...",
            video_title[:50],
        )
        temp_path = r2_service.download_video(r2_key)

        logger.info("Starting Gemini retention analysis for '%s'...", video_title[:50])
        
        # Fetch templates for the channel to provide context to Gemini and for matching
        pacing_service = PacingTemplateService(db)
        templates = await pacing_service.get_templates(channel_id)
        template_dicts = [t.dict() for t in templates]
        
        result = await gemini_service.analyze_video_retention(
            temp_path, video_title, platform, pacing_templates=template_dicts
        )

        # Compute pacing matches
        from app.models.retention_analysis import PacingAnalysis
        try:
            pacing_analysis = PacingAnalysis(**result.get("pacing_analysis", {}))
            duration = result.get("pacing_analysis", {}).get(
                "visual_change_timestamps", [{}]
            )[-1].get("timestamp_seconds") if result.get("pacing_analysis", {}).get("visual_change_timestamps") else None
            
            matches = pacing_service.match_pacing(pacing_analysis, templates, video_duration=duration)
            result["pacing_matches"] = [m.dict() for m in matches]
        except Exception as e:
            logger.warning("Failed to compute pacing matches: %s", e)

        now = now_ist()
        await db.retention_analysis.update_one(
            {"channel_id": channel_id, "video_id": video_id},
            {
                "$set": {
                    "status": "completed",
                    "analysis": result,
                    "duration_seconds": result.get("pacing_analysis", {}).get(
                        "visual_change_timestamps", [{}]
                    )[-1].get("timestamp_seconds") if result.get("pacing_analysis", {}).get("visual_change_timestamps") else None,
                    "analyzed_at": now,
                    "error_message": None,
                    "updated_at": now,
                },
            },
        )
        logger.success(
            "Retention analysis complete for '%s' — predicted retention: %s%%",
            video_title[:50],
            result.get("predicted_avg_retention_percent", "?"),
        )

    except Exception as exc:
        logger.error("Retention analysis failed for '%s': %s", video_title[:50], exc)
        await db.retention_analysis.update_one(
            {"channel_id": channel_id, "video_id": video_id},
            {
                "$set": {
                    "status": "failed",
                    "error_message": str(exc),
                    "updated_at": now_ist(),
                },
            },
        )
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
            logger.info("Cleaned up temp file %s", temp_path)


def compute_comparison(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Compute predicted-vs-actual deviation from a retention_analysis document.

    Returns ``None`` if actuals haven't been backfilled yet.
    """
    if not doc.get("actuals_populated_at"):
        return None

    analysis = doc.get("analysis") or {}
    predicted_retention = analysis.get("predicted_avg_retention_percent")
    actual_retention = doc.get("actual_avg_percentage_viewed")

    retention_deviation: float | None = None
    retention_accuracy_pct: float | None = None
    if predicted_retention is not None and actual_retention is not None:
        retention_deviation = round(predicted_retention - actual_retention, 2)
        retention_accuracy_pct = round(100 - abs(retention_deviation), 2)

    actual_engagement = doc.get("actual_engagement_rate")
    actual_views = doc.get("actual_views")
    actual_views_per_sub = doc.get("actual_views_per_subscriber")

    # Determine qualitative prediction quality
    quality = "unknown"
    if retention_accuracy_pct is not None:
        if retention_accuracy_pct >= 85:
            quality = "accurate"
        elif retention_accuracy_pct >= 70:
            quality = "close"
        else:
            quality = "off"

    return {
        "predicted_avg_retention_percent": predicted_retention,
        "actual_avg_percentage_viewed": actual_retention,
        "retention_deviation": retention_deviation,
        "retention_accuracy_pct": retention_accuracy_pct,
        "actual_engagement_rate": actual_engagement,
        "actual_views": actual_views,
        "actual_views_per_subscriber": actual_views_per_sub,
        "actual_performance_rating": doc.get("actual_performance_rating"),
        "hook_score": analysis.get("hook_analysis", {}).get("score"),
        "pacing_score": analysis.get("pacing_analysis", {}).get("score"),
        "prediction_quality": quality,
        "actuals_populated_at": doc.get("actuals_populated_at"),
    }
