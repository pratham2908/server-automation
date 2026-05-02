"""Retention analysis service — orchestrates video-file analysis via Gemini.

Downloads the video from R2, uploads to Gemini for multimodal analysis,
stores the structured retention prediction, and cleans up temp files.
Also provides a helper for computing predicted-vs-actual deviation.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.error_reporting import report_error
from app.services.gemini import GeminiService
from app.services.pacing_templates import PacingTemplateService
from app.services.r2 import R2Service
from app.timezone import now_ist

logger = get_logger(__name__)


def extract_thumbnail(video_path: str, timestamp: float, output_path: str) -> bool:
    """Extract a high-quality JPEG frame from the video at a specific timestamp using FFMPEG."""
    try:
        # -ss before -i is faster (seeks before decoding)
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(timestamp),
            "-i",
            video_path,
            "-vframes",
            "1",
            "-q:v",
            "2",  # High quality
            output_path,
        ]
        logger.info("Extracting thumbnail at %.2fs: %s", timestamp, " ".join(cmd))
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("FFMPEG extraction failed: %s", e.stderr.decode())
        return False
    except Exception as e:
        logger.error("Failed to extract thumbnail: %s", e)
        return False


async def run_retention_analysis(
    channel_id: str,
    video_id: str,
    db: AsyncIOMotorDatabase,
    r2_service: R2Service,
    gemini_service: GeminiService,
    local_video_path: str | None = None,
) -> None:
    """Analyze a video's retention potential via Gemini multimodal.

    1. Look up the video doc for R2 key, title, and platform.
    2. Mark the ``retention_analysis`` doc as ``analyzing``.
    3. Download from R2 to a temp file.
    4. Send to Gemini for video retention analysis.
    5. Store result (``completed`` or ``failed``).
    6. Clean up the temp file.
    """
    video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
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
    await db.videos.update_one(
        {"channel_id": channel_id, "video_id": video_id},
        {
            "$set": {
                "retention.status": "analyzing",
                "retention.video_title": video_title,
                "retention.platform": platform,
                "retention.error_message": None,
                "retention.updated_at": now,
                "packaging_status": "analyzing",
                "updated_at": now,
            },
            "$setOnInsert": {
                "retention.created_at": now,
            },
        },
    )

    temp_path: str | None = local_video_path
    try:
        if not temp_path:
            logger.info(
                "Downloading video '%s' from R2 for retention analysis...",
                video_title[:50],
            )
            temp_path = r2_service.download_video(r2_key)
        else:
            logger.info("Using local video path for retention analysis: %s", temp_path)

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
            duration = (
                result.get("pacing_analysis", {}).get("visual_change_timestamps", [{}])[-1].get("timestamp_seconds")
                if result.get("pacing_analysis", {}).get("visual_change_timestamps")
                else None
            )

            matches = pacing_service.match_pacing(pacing_analysis, templates, video_duration=duration)
            result["pacing_matches"] = [m.dict() for m in matches]
        except Exception as e:
            logger.warning("Failed to compute pacing matches: %s", e)

        now = now_ist()
        await db.videos.update_one(
            {"channel_id": channel_id, "video_id": video_id},
            {
                "$set": {
                    "retention.status": "completed",
                    "retention.analysis": result,
                    "retention.duration_seconds": result.get("pacing_analysis", {})
                    .get("visual_change_timestamps", [{}])[-1]
                    .get("timestamp_seconds")
                    if result.get("pacing_analysis", {}).get("visual_change_timestamps")
                    else None,
                    "retention.analyzed_at": now,
                    "retention.error_message": None,
                    "retention.updated_at": now,
                },
            },
        )

        # --- PACKAGING LOGIC ---
        packaging = result.get("packaging")
        if packaging:
            logger.info("Processing AI packaging for video %s", video_id)
            updates: dict[str, Any] = {
                "packaging_status": "completed",
                "ai_packaging": packaging,
                "updated_at": now,
            }

            # Thumbnail Extraction
            ts = packaging.get("best_thumbnail_timestamp", 0.0)
            thumbnail_filename = f"thumb_{video_id}.jpg"
            local_thumb_path = f"/tmp/{thumbnail_filename}"

            # Auto-sync metadata to primary fields so it's persisted permanently
            titles = packaging.get("suggested_titles")
            if titles and isinstance(titles, list) and len(titles) > 0:
                updates["title"] = titles[0]

            desc = packaging.get("suggested_description")
            if desc:
                updates["description"] = desc

            tags = packaging.get("suggested_tags")
            if tags:
                # Video model expects list[str]
                if isinstance(tags, str):
                    updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
                else:
                    updates["tags"] = tags

            if extract_thumbnail(temp_path, ts, local_thumb_path):
                try:
                    # Upload to R2 under channel/thumbnails/
                    r2_thumb_key = f"{channel_id}/thumbnails/{video_id}.jpg"

                    with open(local_thumb_path, "rb") as f:
                        r2_service.upload_video(f, r2_thumb_key)

                    # Generate a presigned URL for the frontend to render
                    thumb_url = r2_service.generate_presigned_url(r2_thumb_key, expires_in=604800)  # 7 days
                    updates["ai_packaging"]["thumbnail_url"] = thumb_url
                    logger.success("Thumbnail uploaded and URL generated: %s", thumb_url)
                except Exception as e:
                    logger.error("Failed to upload thumbnail to R2: %s", e)
                finally:
                    if os.path.exists(local_thumb_path):
                        os.unlink(local_thumb_path)

            await db.videos.update_one({"channel_id": channel_id, "video_id": video_id}, {"$set": updates})

        logger.success(
            "Retention analysis complete for '%s' — predicted retention: %s%%",
            video_title[:50],
            result.get("predicted_avg_retention_percent", "?"),
        )

    except Exception as exc:
        logger.error("Retention analysis failed for '%s': %s", video_title[:50], exc)
        await report_error(
            feature="Retention analysis (Gemini)",
            message=f"Retention analysis failed for video '{video_id}': {exc!s}",
            exception=exc,
            context={"channel_id": channel_id, "video_id": video_id},
        )
        now = now_ist()
        await db.videos.update_one(
            {"channel_id": channel_id, "video_id": video_id},
            {
                "$set": {
                    "retention.status": "failed",
                    "retention.error_message": str(exc),
                    "retention.updated_at": now,
                    "packaging_status": "failed",
                    "updated_at": now,
                },
            },
        )
    finally:
        # Only unlink if we DOWNLOADED it (temp_path != local_video_path)
        if temp_path and temp_path != local_video_path and os.path.exists(temp_path):
            os.unlink(temp_path)
            logger.info("Cleaned up temp file %s", temp_path)


def compute_comparison(video: dict[str, Any]) -> dict[str, Any] | None:
    """Compute predicted-vs-actual deviation from a video document.

    Returns ``None`` if actuals haven't been backfilled into `video.retention` yet.
    """
    retention = video.get("retention") or {}
    if not retention.get("actuals_populated_at"):
        return None

    analysis = retention.get("analysis") or {}
    predicted_retention = analysis.get("predicted_avg_retention_percent")
    actual_retention = retention.get("actual_avg_percentage_viewed")

    retention_deviation: float | None = None
    retention_accuracy_pct: float | None = None
    if predicted_retention is not None and actual_retention is not None:
        retention_deviation = round(predicted_retention - actual_retention, 2)
        retention_accuracy_pct = round(100 - abs(retention_deviation), 2)

    actual_engagement = retention.get("actual_engagement_rate")
    actual_views = retention.get("actual_views")
    actual_views_per_sub = retention.get("actual_views_per_subscriber")

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
        "actual_performance_rating": retention.get("actual_performance_rating"),
        "hook_score": analysis.get("hook_analysis", {}).get("score"),
        "pacing_score": analysis.get("pacing_analysis", {}).get("score"),
        "prediction_quality": quality,
        "actuals_populated_at": retention.get("actuals_populated_at"),
    }
