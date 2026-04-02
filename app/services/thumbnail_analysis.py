"""Thumbnail analysis service -- ephemeral image quality and CTR scoring.

Accepts a local image file, runs Gemini multimodal thumbnail analysis,
stores the result in the ``thumbnail_analysis`` collection with a 24-hour
TTL, and cleans up the temp file.
"""

from __future__ import annotations

import os
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.gemini import GeminiService
from app.timezone import now_ist

logger = get_logger(__name__)


async def run_thumbnail_analysis(
    analysis_id: str,
    image_path: str,
    title: str,
    platform: str,
    db: AsyncIOMotorDatabase,
    gemini_service: GeminiService,
) -> None:
    """Analyze a thumbnail image for quality and click-worthiness.

    1. Send the local image to Gemini for multimodal thumbnail analysis.
    2. Store result (``completed`` or ``failed``).
    3. Clean up the temp file.
    """
    try:
        logger.info("Starting thumbnail analysis for '%s'...", title[:50])
        result = await gemini_service.analyze_thumbnail(image_path, title, platform)

        now = now_ist()
        await db.thumbnail_analysis.update_one(
            {"analysis_id": analysis_id},
            {
                "$set": {
                    "status": "completed",
                    "analysis": result,
                    "analyzed_at": now,
                    "error_message": None,
                },
            },
        )
        logger.success(
            "Thumbnail analysis complete for '%s' — overall score: %s",
            title[:50],
            result.get("overall_score", "?"),
        )

    except Exception as exc:
        logger.error("Thumbnail analysis failed for '%s': %s", title[:50], exc)
        await db.thumbnail_analysis.update_one(
            {"analysis_id": analysis_id},
            {
                "$set": {
                    "status": "failed",
                    "error_message": str(exc),
                },
            },
        )
    finally:
        if image_path and os.path.exists(image_path):
            os.unlink(image_path)
            logger.info("Cleaned up thumbnail temp file %s", image_path)


def compute_thumbnail_comparison(
    current_doc: dict[str, Any],
    previous_doc: dict[str, Any],
) -> dict[str, Any] | None:
    """Compute deltas between two thumbnail analysis results.

    Returns ``None`` if either analysis is not completed.
    """
    cur = current_doc.get("analysis")
    prev = previous_doc.get("analysis")

    if not cur or not prev:
        return None
    if current_doc.get("status") != "completed" or previous_doc.get("status") != "completed":
        return None

    def _delta(key: str) -> float | None:
        c, p = cur.get(key), prev.get(key)
        if c is not None and p is not None:
            return round(c - p, 2)
        return None

    overall_delta = _delta("overall_score")

    return {
        "previous_analysis_id": previous_doc.get("analysis_id"),
        "previous_label": previous_doc.get("label"),
        "overall_score_delta": overall_delta,
        "composition_delta": _delta("composition_score"),
        "text_readability_delta": _delta("text_readability_score"),
        "emotional_impact_delta": _delta("emotional_impact_score"),
        "face_visibility_delta": _delta("face_visibility_score"),
        "contrast_color_delta": _delta("contrast_color_score"),
        "ctr_prediction_delta": _delta("ctr_prediction"),
        "improved": overall_delta > 0 if overall_delta is not None else None,
    }
