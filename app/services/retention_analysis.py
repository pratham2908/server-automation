"""Retention analysis service — orchestrates video-file analysis via Gemini.

Downloads the video from R2, uploads to Gemini for multimodal analysis,
stores the structured retention prediction, and cleans up temp files.
Also provides a helper for computing predicted-vs-actual deviation.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from typing import Any

from dateutil.parser import isoparse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_channel_platform
from app.logger import get_logger
from app.services.error_reporting import report_error
from app.services.gemini import GeminiService
from app.services.pacing_templates import PacingTemplateService
from app.services.r2 import R2Service
from app.services.schedule_operation import schedule_single_video_instagram
from app.timezone import IST, now_ist

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


async def promote_processing_to_ready(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    video_id: str,
) -> None:
    """Make a single fully-analysed video postable.

    Every upload path now creates a video as ``processing`` and leaves it there
    until AI packaging has written its title/description/tags — that packaging is
    the source of truth for a video's metadata, so it is not postable before it
    exists. Once packaging has *completed*, this promotes the video:

    * to ``ready`` (and onto the posting queue), or
    * for an Instagram video carrying a future ``scheduled_at`` (a schedule set at
      upload time), straight into the schedule queue via the normal scheduling
      path — exactly what a manual schedule of a ready video would do.

    Two guards make it safe to call unconditionally after any analysis:

    * ``status == "processing"`` — never rewind a live/scheduled/published video,
      e.g. a manual "predict" re-analysing an existing upload.
    * ``packaging_status == "completed"`` — a video whose analysis produced no
      packaging stays ``processing`` rather than being posted without metadata.

    It acts on one video; callers invoke it for each record (primary and each
    multi-channel sibling) once that record's packaging is done.
    """
    video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if not video or video.get("status") != "processing" or video.get("packaging_status") != "completed":
        return

    now = now_ist()
    scheduled_at = video.get("scheduled_at")
    if isinstance(scheduled_at, str):
        try:
            scheduled_at = isoparse(scheduled_at)
        except (ValueError, TypeError):
            scheduled_at = None
    if isinstance(scheduled_at, datetime) and scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=IST)

    is_scheduled = isinstance(scheduled_at, datetime) and scheduled_at > now
    if is_scheduled:
        channel = await db.channels.find_one({"channel_id": channel_id})
        # Only Instagram honours an upload-time schedule (as the create paths did);
        # a YouTube schedule falls through to "ready" and is set via the scheduler.
        if get_channel_platform(channel or {}) == "instagram":
            await schedule_single_video_instagram(
                db=db, channel_id=channel_id, video_doc=video, scheduled_at=scheduled_at
            )
            return

    await db.videos.update_one(
        {"channel_id": channel_id, "video_id": video_id, "status": "processing"},
        {"$set": {"status": "ready", "updated_at": now}},
    )
    last = await db.posting_queue.find_one({"channel_id": channel_id}, sort=[("position", -1)])
    await db.posting_queue.insert_one(
        {
            "channel_id": channel_id,
            "video_id": video_id,
            "position": (last["position"] + 1) if last else 1,
            "added_at": now,
        }
    )


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

            thumb_url: str | None = None
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
            # Packaging is now written for the primary — it can become postable.
            await promote_processing_to_ready(db, channel_id, video_id)

            # --- MULTI-CHANNEL SIBLING PROPAGATION ---
            # If this video belongs to a multi-channel group, generate
            # platform-appropriate packaging for every sibling record using
            # the analysis result we already have (no re-upload needed).
            primary_doc = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
            group_id = (primary_doc or {}).get("multi_channel_group_id")
            if group_id:
                siblings = await db.videos.find(
                    {
                        "multi_channel_group_id": group_id,
                        "video_id": {"$ne": video_id},
                    }
                ).to_list(length=None)

                for sibling in siblings:
                    sib_channel_id = sibling["channel_id"]
                    sib_video_id = sibling["video_id"]
                    try:
                        sib_channel = await db.channels.find_one({"channel_id": sib_channel_id})
                        sib_platform = (sib_channel or {}).get("platform", "youtube")
                        sib_name = (sib_channel or {}).get("name", "")
                        sib_default_desc = (sib_channel or {}).get("default_description", "")
                        sib_default_tags = (sib_channel or {}).get("default_tags") or []

                        sib_packaging = await gemini_service.generate_platform_packaging(
                            analysis_result=result,
                            platform=sib_platform,
                            channel_name=sib_name,
                            default_description=sib_default_desc,
                            default_tags=sib_default_tags,
                        )
                        if thumb_url:
                            sib_packaging["thumbnail_url"] = thumb_url

                        sib_updates: dict[str, Any] = {
                            "packaging_status": "completed",
                            "ai_packaging": sib_packaging,
                            "updated_at": now,
                        }
                        sib_titles = sib_packaging.get("suggested_titles")
                        if sib_titles and isinstance(sib_titles, list) and sib_titles:
                            sib_updates["title"] = sib_titles[0]
                        sib_desc = sib_packaging.get("suggested_description")
                        if sib_desc:
                            sib_updates["description"] = sib_desc
                        sib_tags = sib_packaging.get("suggested_tags")
                        if sib_tags:
                            sib_updates["tags"] = (
                                sib_tags
                                if isinstance(sib_tags, list)
                                else [t.strip() for t in sib_tags.split(",") if t.strip()]
                            )

                        await db.videos.update_one(
                            {"channel_id": sib_channel_id, "video_id": sib_video_id},
                            {"$set": sib_updates},
                        )
                        await promote_processing_to_ready(db, sib_channel_id, sib_video_id)
                        logger.info("Propagated packaging to sibling %s (%s)", sib_video_id, sib_platform)
                    except Exception as e:
                        logger.error("Failed to generate packaging for sibling %s: %s", sib_video_id, e)
                        await db.videos.update_one(
                            {"channel_id": sib_channel_id, "video_id": sib_video_id},
                            {"$set": {"packaging_status": "failed", "updated_at": now}},
                        )

        else:
            # The retention pass succeeded but returned no packaging, so no
            # title/description/tags were generated. The video has no metadata to
            # post with, so it must not become "ready": mark packaging failed
            # (rather than leaving it stuck "analyzing") and leave it "processing"
            # for a retry. Promotion is gated on packaging_status == "completed",
            # so it is correctly skipped here.
            logger.warning("Analysis for video %s returned no packaging — marking packaging failed", video_id)
            await db.videos.update_one(
                {"channel_id": channel_id, "video_id": video_id},
                {"$set": {"packaging_status": "failed", "updated_at": now}},
            )

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
