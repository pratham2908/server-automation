"""Core schedule operation: upload a video to a platform and update all DB state.

The ``schedule`` endpoint (videos router) delegates to
:func:`schedule_single_video` so the logic lives in one place.
Supports both YouTube (immediate upload with ``publishAt``) and
Instagram (queued for the background auto-publisher).
"""

import os
from datetime import datetime
from typing import Any

import pytz

from app.logger import get_logger
from app.timezone import IST, now_ist, to_ist_iso

logger = get_logger(__name__)


def _build_instagram_caption(video_doc: dict[str, Any]) -> str:
    """Combine title, description, and tags into a single Instagram caption."""
    parts: list[str] = []

    title = video_doc.get("title", "").strip()
    if title:
        parts.append(title)

    desc = (video_doc.get("description") or "").strip()
    if desc:
        parts.append(desc)

    tags = video_doc.get("tags") or []
    if tags:
        hashtags = " ".join(
            f"#{t.strip().replace(' ', '')}" if not t.startswith("#") else t.strip()
            for t in tags
            if t.strip()
        )
        if hashtags:
            parts.append(hashtags)

    return "\n\n".join(parts)


async def _move_to_schedule_queue(
    db, channel_id: str, video_id: str, scheduled_at: datetime,
) -> None:
    """Remove from ready queue and insert into scheduled queue."""
    now = now_ist()

    await db.posting_queue.delete_one(
        {"channel_id": channel_id, "video_id": video_id}
    )

    last = await db.schedule_queue.find_one(
        {"channel_id": channel_id},
        sort=[("position", -1)],
    )
    next_pos = (last["position"] + 1) if last else 1

    await db.schedule_queue.insert_one(
        {
            "channel_id": channel_id,
            "video_id": video_id,
            "position": next_pos,
            "scheduled_at": scheduled_at,
            "added_at": now,
        }
    )


async def schedule_single_video(
    *,
    db,
    r2_service,
    youtube_service,
    channel_id: str,
    video_doc: dict[str, Any],
    scheduled_at: datetime,
) -> dict[str, Any]:
    """Upload a single video to YouTube with a ``publishAt`` time.

    On success the function:
    1. Sets ``youtube_video_id`` and status -> ``scheduled`` on the video doc.
    2. Removes the video from the ready queue (``posting_queue``).
    3. Inserts an entry into the scheduled queue (``schedule_queue``).

    Returns a result dict with ``"status"`` of ``"scheduled"`` or ``"failed"``.
    """
    video_id = video_doc["video_id"]

    if not video_doc.get("r2_object_key"):
        return {
            "video_id": video_id,
            "status": "skipped",
            "reason": "no R2 key",
        }

    # Convert scheduled_at to UTC ISO string for YouTube's publishAt.
    if scheduled_at.tzinfo is not None:
        utc_dt = scheduled_at.astimezone(pytz.utc)
    else:
        utc_dt = scheduled_at.replace(tzinfo=IST).astimezone(pytz.utc)
    publish_at_str = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    tmp_path = None
    try:
        tmp_path = r2_service.download_video(video_doc["r2_object_key"])

        yt_id = youtube_service.upload_video(
            file_path=tmp_path,
            title=video_doc.get("title", ""),
            description=video_doc.get("description", ""),
            tags=video_doc.get("tags", []),
            publish_at=publish_at_str,
        )

        now = now_ist()

        await db.videos.update_one(
            {"channel_id": channel_id, "video_id": video_id},
            {
                "$set": {
                    "youtube_video_id": yt_id,
                    "status": "scheduled",
                    "scheduled_at": scheduled_at,
                    "updated_at": now,
                }
            },
        )

        await _move_to_schedule_queue(db, channel_id, video_id, scheduled_at)

        logger.success(
            "Scheduled '%s' on YouTube (yt_id=%s) for %s",
            video_doc.get("title", video_id),
            yt_id,
            publish_at_str,
        )

        return {
            "video_id": video_id,
            "status": "scheduled",
            "youtube_video_id": yt_id,
            "scheduled_at": to_ist_iso(scheduled_at),
        }

    except Exception:
        logger.exception("Failed to schedule video %s on YouTube", video_id)
        return {"video_id": video_id, "status": "failed"}

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def schedule_single_video_instagram(
    *,
    db,
    channel_id: str,
    video_doc: dict[str, Any],
    scheduled_at: datetime,
) -> dict[str, Any]:
    """Queue a video for Instagram Reel publishing at ``scheduled_at``.

    Unlike YouTube (which accepts ``publishAt``), Instagram publishes
    immediately.  So this function only updates DB state — the actual
    upload + publish happens in the background auto-publisher when
    ``scheduled_at`` arrives.

    On success:
    1. Sets status -> ``scheduled`` and ``scheduled_at`` on the video doc.
    2. Removes from the ready queue, inserts into the scheduled queue.
    """
    video_id = video_doc["video_id"]

    if not video_doc.get("r2_object_key"):
        return {
            "video_id": video_id,
            "status": "skipped",
            "reason": "no R2 key",
        }

    try:
        now = now_ist()

        await db.videos.update_one(
            {"channel_id": channel_id, "video_id": video_id},
            {
                "$set": {
                    "status": "scheduled",
                    "scheduled_at": scheduled_at,
                    "updated_at": now,
                }
            },
        )

        await _move_to_schedule_queue(db, channel_id, video_id, scheduled_at)

        logger.success(
            "Scheduled '%s' for Instagram publish at %s",
            video_doc.get("title", video_id),
            to_ist_iso(scheduled_at),
        )

        return {
            "video_id": video_id,
            "status": "scheduled",
            "scheduled_at": to_ist_iso(scheduled_at),
        }

    except Exception:
        logger.exception("Failed to schedule video %s for Instagram", video_id)
        return {"video_id": video_id, "status": "failed"}
