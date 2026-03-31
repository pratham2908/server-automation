"""Preview analysis service -- ephemeral video retention prediction.

Accepts a local video file (not from R2), runs the same Gemini multimodal
retention analysis, stores the result in the ``preview_analysis`` collection
with a 24-hour TTL, and cleans up the temp file.
"""

from __future__ import annotations

import os
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.gemini import GeminiService
from app.timezone import now_ist

logger = get_logger(__name__)


async def run_preview_analysis(
    preview_id: str,
    video_path: str,
    title: str,
    platform: str,
    db: AsyncIOMotorDatabase,
    gemini_service: GeminiService,
) -> None:
    """Analyze a video file for retention prediction (ephemeral preview).

    1. Mark the doc as ``analyzing``.
    2. Send the local file to Gemini for multimodal retention analysis.
    3. Store result (``completed`` or ``failed``).
    4. Clean up the temp file.
    """
    try:
        logger.info("Starting preview retention analysis for '%s'...", title[:50])
        result = await gemini_service.analyze_video_retention(
            video_path, title, platform,
        )

        now = now_ist()
        await db.preview_analysis.update_one(
            {"preview_id": preview_id},
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
            "Preview analysis complete for '%s' — predicted retention: %s%%",
            title[:50],
            result.get("predicted_avg_retention_percent", "?"),
        )

    except Exception as exc:
        logger.error("Preview analysis failed for '%s': %s", title[:50], exc)
        await db.preview_analysis.update_one(
            {"preview_id": preview_id},
            {
                "$set": {
                    "status": "failed",
                    "error_message": str(exc),
                },
            },
        )
    finally:
        if video_path and os.path.exists(video_path):
            os.unlink(video_path)
            logger.info("Cleaned up preview temp file %s", video_path)


def compute_version_comparison(
    current_doc: dict[str, Any],
    previous_doc: dict[str, Any],
) -> dict[str, Any] | None:
    """Compute deltas between two preview analysis results.

    Returns ``None`` if either analysis is not completed.
    """
    cur_analysis = current_doc.get("analysis")
    prev_analysis = previous_doc.get("analysis")

    if not cur_analysis or not prev_analysis:
        return None
    if current_doc.get("status") != "completed" or previous_doc.get("status") != "completed":
        return None

    cur_retention = cur_analysis.get("predicted_avg_retention_percent")
    prev_retention = prev_analysis.get("predicted_avg_retention_percent")

    cur_hook = (cur_analysis.get("hook_analysis") or {}).get("score")
    prev_hook = (prev_analysis.get("hook_analysis") or {}).get("score")

    cur_pacing = (cur_analysis.get("pacing_analysis") or {}).get("pacing_score")
    prev_pacing = (prev_analysis.get("pacing_analysis") or {}).get("pacing_score")

    retention_delta = round(cur_retention - prev_retention, 2) if cur_retention is not None and prev_retention is not None else None
    hook_delta = (cur_hook - prev_hook) if cur_hook is not None and prev_hook is not None else None
    pacing_delta = (cur_pacing - prev_pacing) if cur_pacing is not None and prev_pacing is not None else None

    return {
        "previous_preview_id": previous_doc.get("preview_id"),
        "previous_label": previous_doc.get("label"),
        "predicted_retention_delta": retention_delta,
        "hook_score_delta": hook_delta,
        "pacing_score_delta": pacing_delta,
        "improved": retention_delta > 0 if retention_delta is not None else None,
    }
