"""Core schedule operation: upload a video to a platform and update all DB state.

The ``schedule`` endpoint (videos router) delegates to
:func:`enqueue_video_for_youtube` or :func:`schedule_single_video_instagram`
depending on the channel's platform. YouTube uploads immediately with
``publishAt``; Instagram is queued for the background auto-publisher.
"""

from datetime import datetime
from typing import Any

from app.logger import get_logger
from app.timezone import now_ist, to_ist_iso

logger = get_logger(__name__)


def _build_instagram_caption(video_doc: dict[str, Any]) -> str:
    """Combine title, description, and tags into a single Instagram caption."""
    parts: list[str] = []

    title = video_doc.get("title", "").strip()
    if title:
        parts.append(title)

    desc = (video_doc.get("description") or "").strip()
    if desc:
        # Normalise line breaks to \n only
        desc = desc.replace("\r\n", "\n").replace("\r", "\n")
        parts.append(desc)

    tags = video_doc.get("tags") or []
    if tags:
        # Create hashtags string, but check if they're already in desc
        hashtag_list = []
        for t in tags:
            tag = t.strip()
            if not tag:
                continue
            h = f"#{tag.replace(' ', '')}" if not tag.startswith("#") else tag
            if h.lower() not in desc.lower():
                hashtag_list.append(h)

        if hashtag_list:
            parts.append(" ".join(hashtag_list))

    return "\n\n".join(parts)


async def _move_to_schedule_queue(
    db,
    channel_id: str,
    video_id: str,
    scheduled_at: datetime,
    platform: str = "youtube",
) -> None:
    """Remove from ready queue and insert into scheduled queue."""
    now = now_ist()

    await db.posting_queue.delete_one({"channel_id": channel_id, "video_id": video_id})

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
            "platform": platform,
            "added_at": now,
        }
    )


async def enqueue_video_for_youtube(
    *,
    db,
    channel_id: str,
    video_doc: dict[str, Any],
    scheduled_at: datetime,
) -> dict[str, Any]:
    """Non-blocking: add a YouTube video to the schedule queue for the background uploader.

    Returns immediately after updating DB state. The actual R2 download
    and YouTube upload is handled by :func:`run_youtube_uploader`.

    On success:
    1. Sets status -> ``queued`` and ``scheduled_at`` on the video doc.
    2. Removes from the ready queue, inserts into the schedule queue (platform=youtube).
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
                    "status": "queued",
                    "scheduled_at": scheduled_at,
                    "updated_at": now,
                }
            },
        )

        await _move_to_schedule_queue(db, channel_id, video_id, scheduled_at, platform="youtube")

        logger.success(
            "Enqueued '%s' for YouTube upload at %s",
            video_doc.get("title", video_id),
            to_ist_iso(scheduled_at),
        )

        return {
            "video_id": video_id,
            "status": "queued",
            "scheduled_at": to_ist_iso(scheduled_at),
        }

    except Exception:
        logger.exception("Failed to enqueue video %s for YouTube", video_id)
        return {"video_id": video_id, "status": "failed"}


async def schedule_single_video_instagram(
    *,
    db,
    channel_id: str,
    video_doc: dict[str, Any],
    scheduled_at: datetime,
) -> dict[str, Any]:
    """Queue an Instagram Reel for publishing at ``scheduled_at``.

    Unlike YouTube (which accepts ``publishAt``), Instagram publishes
    immediately via the auto-publisher.  This function only updates DB state —
    the actual upload + publish happens when ``scheduled_at`` arrives.

    On success:
    1. Sets status -> ``queued`` and ``scheduled_at`` on the video doc.
    2. Removes from the ready queue, inserts into the schedule queue (platform=instagram).
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
                    "status": "queued",
                    "scheduled_at": scheduled_at,
                    "updated_at": now,
                }
            },
        )

        await _move_to_schedule_queue(db, channel_id, video_id, scheduled_at, platform="instagram")

        logger.success(
            "Queued '%s' for Instagram publish at %s",
            video_doc.get("title", video_id),
            to_ist_iso(scheduled_at),
        )

        return {
            "video_id": video_id,
            "status": "queued",
            "scheduled_at": to_ist_iso(scheduled_at),
        }

    except Exception:
        logger.exception("Failed to queue video %s for Instagram", video_id)
        return {"video_id": video_id, "status": "failed"}
