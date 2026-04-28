"""Background YouTube auto-uploader.

YouTube videos queued via ``POST /{video_id}/schedule`` land in the
``schedule_queue`` collection with status ``queued``.  This module runs an
async loop that polls every 5 minutes, downloads each video from R2, and
uploads it to YouTube (private + ``publishAt``).

On success  → status: ``queued``  →  ``scheduled``  (youtube_video_id set)
On failure  → attempt_count incremented; after MAX_ATTEMPTS resets to ``ready``
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytz

from app.logger import get_logger
from app.timezone import IST, UTC, now_ist, to_ist_iso

logger = get_logger(__name__)

_POLL_INTERVAL_SECONDS = 300   # 5 minutes
_MAX_UPLOAD_ATTEMPTS = 5


async def _upload_one_video(
    *,
    db: Any,
    r2_service: Any,
    youtube_service: Any,
    channel_id: str,
    video_doc: dict[str, Any],
    queue_entry: dict[str, Any],
) -> bool:
    """Download from R2, upload to YouTube, update DB state.  Returns ``True`` on success."""
    video_id = video_doc["video_id"]
    scheduled_at = queue_entry.get("scheduled_at")

    r2_key = video_doc.get("r2_object_key")
    if not r2_key:
        logger.error(
            "YouTube uploader: video '%s' has no r2_object_key — removing stale queue entry",
            video_id,
        )
        await db.schedule_queue.delete_one({"_id": queue_entry["_id"]})
        return False

    # Convert scheduled_at to UTC ISO string for YouTube's publishAt.
    publish_at_str = None
    if scheduled_at:
        if scheduled_at.tzinfo is not None:
            utc_dt = scheduled_at.astimezone(UTC)
        else:
            # MongoDB stores naive datetimes as UTC.
            utc_dt = scheduled_at.replace(tzinfo=UTC)
        
        # Only set publish_at if it's actually in the future.
        if utc_dt > now_ist().astimezone(UTC):
            publish_at_str = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    tmp_path = None
    try:
        tmp_path = r2_service.download_video(r2_key)

        yt_id = youtube_service.upload_video(
            file_path=tmp_path,
            title=video_doc.get("title", ""),
            description=video_doc.get("description", ""),
            tags=video_doc.get("tags", []),
            privacy_status="public" if not publish_at_str else "private",
            publish_at=publish_at_str,
        )

        now = now_ist()

        await db.videos.update_one(
            {"channel_id": channel_id, "video_id": video_id},
            {
                "$set": {
                    "youtube_video_id": yt_id,
                    "status": "scheduled",
                    "updated_at": now,
                }
            },
        )

        await db.schedule_queue.delete_one({"_id": queue_entry["_id"]})

        logger.success(
            "YouTube uploader: uploaded '%s' (yt_id=%s) scheduled for %s",
            video_doc.get("title", video_id)[:50],
            yt_id,
            publish_at_str or "immediate",
        )
        return True

    except Exception:
        attempts = queue_entry.get("attempt_count", 0) + 1
        logger.exception(
            "YouTube uploader: failed for video '%s' on channel '%s' (attempt %d/%d)",
            video_id,
            channel_id,
            attempts,
            _MAX_UPLOAD_ATTEMPTS,
        )

        if attempts >= _MAX_UPLOAD_ATTEMPTS:
            logger.error(
                "YouTube uploader: giving up on video '%s' after %d attempts — resetting to 'ready'",
                video_id,
                _MAX_UPLOAD_ATTEMPTS,
            )
            now = now_ist()
            await db.schedule_queue.delete_one({"_id": queue_entry["_id"]})
            await db.videos.update_one(
                {"channel_id": channel_id, "video_id": video_id},
                {
                    "$set": {
                        "status": "ready",
                        "scheduled_at": None,
                        "updated_at": now,
                    }
                },
            )
            # Restore posting_queue entry so it can be re-scheduled
            last = await db.posting_queue.find_one(
                {"channel_id": channel_id}, sort=[("position", -1)]
            )
            next_pos = (last["position"] + 1) if last else 1
            await db.posting_queue.insert_one(
                {
                    "channel_id": channel_id,
                    "video_id": video_id,
                    "position": next_pos,
                    "added_at": now,
                }
            )
        else:
            await db.schedule_queue.update_one(
                {"_id": queue_entry["_id"]},
                {"$set": {"attempt_count": attempts}},
            )

        return False

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def _poll_and_upload(db: Any, r2_service: Any) -> None:
    """One poll cycle: find all queued YouTube entries and upload them."""
    import app.main as main_app  # Lazy import to avoid circular dependency

    logger.info("YouTube uploader: checking schedule queue...")

    # Only pick up entries explicitly marked as youtube (or missing platform).
    # We pick them up immediately so we can schedule them natively on YouTube.
    queued_entries = await db.schedule_queue.find(
        {
            "$or": [{"platform": "youtube"}, {"platform": {"$exists": False}}]
        }
    ).to_list(length=None)

    if not queued_entries:
        logger.info("YouTube uploader: no due YouTube entries.")
        return

    logger.info("YouTube uploader: found %d queued YouTube entry(s)", len(queued_entries))

    for entry in queued_entries:
        channel_id = entry.get("channel_id", "")
        video_id = entry.get("video_id", "")

        channel_doc = await db.channels.find_one({"channel_id": channel_id})
        if not channel_doc:
            logger.warning(
                "YouTube uploader: channel '%s' not found — removing stale entry", channel_id
            )
            await db.schedule_queue.delete_one({"_id": entry["_id"]})
            continue

        # Only process YouTube channels
        platform = channel_doc.get("platform", "youtube")
        if platform != "youtube":
            continue

        video_doc = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
        if not video_doc:
            logger.warning(
                "YouTube uploader: video '%s' not found — removing stale entry", video_id
            )
            await db.schedule_queue.delete_one({"_id": entry["_id"]})
            continue

        # Guard: only process videos in queued state
        if video_doc.get("status") not in ("queued",):
            logger.warning(
                "YouTube uploader: video '%s' status is '%s', not 'queued' — skipping",
                video_id,
                video_doc.get("status"),
            )
            continue

        if not main_app.youtube_service_manager:
            logger.error("YouTube uploader: YouTubeServiceManager not available")
            continue

        yt_service = await main_app.youtube_service_manager.get_service(channel_id)
        if not yt_service:
            logger.error(
                "YouTube uploader: no YouTube service for channel '%s' — will retry next poll",
                channel_id,
            )
            continue

        await _upload_one_video(
            db=db,
            r2_service=r2_service,
            youtube_service=yt_service,
            channel_id=channel_id,
            video_doc=video_doc,
            queue_entry=entry,
        )


async def run_youtube_uploader(db: Any, r2_service: Any) -> None:
    """Long-running loop that uploads queued YouTube videos every 5 minutes."""
    logger.info("YouTube uploader started (poll interval: %ds)", _POLL_INTERVAL_SECONDS)

    while True:
        try:
            await _poll_and_upload(db, r2_service)
        except asyncio.CancelledError:
            logger.info("YouTube uploader shutting down")
            break
        except Exception:
            logger.exception("YouTube uploader encountered an error during poll cycle")

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
