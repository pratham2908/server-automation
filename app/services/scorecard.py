"""Pre-publish scorecard service -- unified readiness assessment.

Aggregates all available pre-publish signals (retention prediction,
thumbnail analysis, channel patterns, content param alignment, etc.)
and sends them to Gemini for a combined readiness verdict.
"""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.gemini import GeminiService

logger = get_logger(__name__)


async def _gather_retention_signal(
    db: AsyncIOMotorDatabase, channel_id: str, video_id: str,
) -> dict[str, Any] | None:
    """Pull the latest retention analysis for the video, if available."""
    doc = await db.retention_analysis.find_one(
        {"channel_id": channel_id, "video_id": video_id, "status": "completed"},
    )
    if not doc or not doc.get("analysis"):
        return None

    a = doc["analysis"]
    return {
        "predicted_avg_retention_percent": a.get("predicted_avg_retention_percent"),
        "hook_score": (a.get("hook_analysis") or {}).get("score"),
        "hook_risk_level": (a.get("hook_analysis") or {}).get("risk_level"),
        "hook_notes": (a.get("hook_analysis") or {}).get("notes", []),
        "pacing_score": (a.get("pacing_analysis") or {}).get("pacing_score"),
        "avg_cut_interval": (a.get("pacing_analysis") or {}).get("avg_cut_interval_seconds"),
        "drop_off_count": len(a.get("predicted_drop_off_points", [])),
        "top_drop_off": (
            a["predicted_drop_off_points"][0]
            if a.get("predicted_drop_off_points")
            else None
        ),
        "strengths": a.get("strengths", []),
        "weaknesses": a.get("weaknesses", []),
    }


async def _gather_thumbnail_signal(
    db: AsyncIOMotorDatabase, channel_id: str, video_id: str,
) -> dict[str, Any] | None:
    """Pull the latest completed thumbnail analysis linked to this video."""
    doc = await db.thumbnail_analysis.find_one(
        {"channel_id": channel_id, "video_id": video_id, "status": "completed"},
        sort=[("created_at", -1)],
    )
    if not doc or not doc.get("analysis"):
        return None

    a = doc["analysis"]
    return {
        "overall_score": a.get("overall_score"),
        "composition_score": a.get("composition_score"),
        "text_readability_score": a.get("text_readability_score"),
        "emotional_impact_score": a.get("emotional_impact_score"),
        "face_visibility_score": a.get("face_visibility_score"),
        "contrast_color_score": a.get("contrast_color_score"),
        "ctr_prediction": a.get("ctr_prediction"),
        "click_worthiness": a.get("click_worthiness"),
        "weaknesses": a.get("weaknesses", []),
    }


async def _gather_channel_patterns(
    db: AsyncIOMotorDatabase, channel_id: str,
) -> dict[str, Any] | None:
    """Pull channel-level analysis data (best times, combinations, etc.)."""
    doc = await db.analysis.find_one({"channel_id": channel_id})
    if not doc:
        return None

    return {
        "best_posting_times": doc.get("best_posting_times", []),
        "best_combinations": doc.get("best_combinations", []),
        "category_analysis": doc.get("category_analysis", []),
        "content_param_analysis": doc.get("content_param_analysis", []),
    }


def _build_content_alignment_signal(
    video: dict, channel_patterns: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Check how well the video's content params match channel patterns."""
    if not channel_patterns:
        return None

    video_category = video.get("category", "")
    video_params = video.get("content_params") or {}

    cat_scores = {
        c["category"]: c.get("score", 0)
        for c in channel_patterns.get("category_analysis", [])
    }

    param_analysis = channel_patterns.get("content_param_analysis", [])
    param_best = {
        p["param_name"]: p.get("best_values", [])
        for p in param_analysis
    }

    matching_best_values = 0
    total_params = len(video_params)
    for param_name, param_value in video_params.items():
        if param_value in param_best.get(param_name, []):
            matching_best_values += 1

    return {
        "video_category": video_category,
        "category_score": cat_scores.get(video_category),
        "content_params": video_params,
        "params_matching_best_values": matching_best_values,
        "total_params": total_params,
        "best_combinations": channel_patterns.get("best_combinations", []),
    }


def _build_title_description_signal(video: dict) -> dict[str, Any]:
    """Extract title/description metadata for Gemini to evaluate."""
    title = video.get("title", "")
    description = video.get("description", "")
    tags = video.get("tags", [])

    return {
        "title": title,
        "title_length": len(title),
        "description": description[:500],
        "description_length": len(description),
        "tags": tags[:20],
        "tag_count": len(tags),
    }


async def generate_scorecard(
    channel_id: str,
    video_id: str,
    db: AsyncIOMotorDatabase,
    gemini_service: GeminiService,
    *,
    platform: str = "youtube",
) -> dict[str, Any]:
    """Generate a pre-publish scorecard for a video.

    Gathers all available signals, sends them to Gemini, and returns
    the synthesized scorecard with per-dimension scores, top issues,
    and a publish recommendation.
    """
    video = await db.videos.find_one(
        {"channel_id": channel_id, "video_id": video_id},
    )
    if not video:
        raise ValueError(f"Video '{video_id}' not found for channel '{channel_id}'")

    retention = await _gather_retention_signal(db, channel_id, video_id)
    thumbnail = await _gather_thumbnail_signal(db, channel_id, video_id)
    channel_patterns = await _gather_channel_patterns(db, channel_id)
    content_alignment = _build_content_alignment_signal(video, channel_patterns)
    title_desc = _build_title_description_signal(video)

    posting_time_signal = None
    if channel_patterns and channel_patterns.get("best_posting_times"):
        posting_time_signal = {
            "best_posting_times": channel_patterns["best_posting_times"],
            "scheduled_at": str(video.get("scheduled_at", "")) if video.get("scheduled_at") else None,
        }

    signals: dict[str, Any] = {
        "video_title": video.get("title", ""),
        "video_category": video.get("category", ""),
        "video_status": video.get("status", ""),
        "title_description": title_desc,
    }
    if retention:
        signals["retention"] = retention
    if thumbnail:
        signals["thumbnail"] = thumbnail
    if content_alignment:
        signals["content_alignment"] = content_alignment
    if posting_time_signal:
        signals["posting_time"] = posting_time_signal

    logger.info(
        "Generating scorecard for '%s' — signals available: %s",
        video.get("title", "")[:40],
        [k for k in signals if k not in ("video_title", "video_category", "video_status")],
    )

    result = await gemini_service.generate_scorecard(signals, platform)

    result["video_id"] = video_id
    result["channel_id"] = channel_id
    result["signals_used"] = [
        k for k in signals if k not in ("video_title", "video_category", "video_status")
    ]

    return result
