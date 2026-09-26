"""The comment posted on a video the moment it goes live.

The usual reason to want one is to keep hashtags or a CTA out of the caption.
People ask for it as a "pinned comment", but neither platform exposes pinning
to an API — Instagram's comment node supports only read, delete, and hide, and
YouTube has no pin endpoint either. Being *first* is what is actually
achievable, so that is what this does.

The two platforms differ in when "right after publish" happens:

* Instagram publishes when our auto-publisher says so, so the comment goes up
  in the same breath as the reel.
* YouTube uploads ahead of time and publishes itself at ``publishAt``, while
  the video sits private until then. A comment on a private video is rejected,
  so a scheduled upload leaves the comment pending and a sweep posts it once
  the scheduled time has passed.

A comment is a nicety attached to a publish, never the point of it. Nothing
here is allowed to fail a publish or an upload that otherwise succeeded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.logger import get_logger
from app.timezone import now_ist

logger = get_logger(__name__)

PENDING = "pending"
POSTED = "posted"
FAILED = "failed"

# Platform-enforced caption limits; exceeding them is a hard API error, so it is
# worth refusing at the edge where the user can still fix the text.
MAX_LENGTH: dict[str, int] = {"instagram": 2200, "youtube": 10000}

# Matches the publish/upload workers: enough to ride out a transient API error,
# few enough that a permanently rejected comment stops burning quota.
MAX_ATTEMPTS = 5

# The video id field to comment against, per platform.
_PLATFORM_ID_FIELD = {"instagram": "instagram_media_id", "youtube": "youtube_video_id"}


def validate_comment(text: str | None, platform: str) -> str | None:
    """Normalise a user-supplied comment, or raise if it cannot be posted.

    Returns ``None`` for "no comment wanted", which is distinct from invalid:
    blank input means the field was simply left empty.
    """
    if text is None:
        return None

    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return None

    limit = MAX_LENGTH.get(platform)
    if limit is not None and len(cleaned) > limit:
        raise ValueError(f"A {platform} comment cannot exceed {limit} characters (got {len(cleaned)})")

    return cleaned


def platform_id_field(platform: str) -> str | None:
    """The video-doc field holding the platform's own id for this video."""
    return _PLATFORM_ID_FIELD.get(platform)


def is_due(video_doc: dict[str, Any], platform: str, now: datetime) -> bool:
    """Whether a pending comment on *video_doc* can be posted yet.

    A YouTube video scheduled into the future is still private, so its comment
    waits. Everything already past its scheduled time is fair game, whether or
    not a sync has noticed the video going public.
    """
    if video_doc.get("first_comment_status") != PENDING:
        return False
    if not (video_doc.get("first_comment") or "").strip():
        return False
    if video_doc.get("first_comment_attempts", 0) >= MAX_ATTEMPTS:
        return False

    id_field = platform_id_field(platform)
    if not id_field or not video_doc.get(id_field):
        return False

    scheduled_at = video_doc.get("scheduled_at")
    if isinstance(scheduled_at, datetime) and scheduled_at > now:
        return False

    return True


def pending_fields(comment: str | None) -> dict[str, Any]:
    """The video-doc fields that arm (or disarm) a first comment.

    Clearing matters as much as setting: rescheduling a video whose comment
    already failed should give it a fresh run rather than inherit the old
    attempt count.
    """
    if not comment:
        return {
            "first_comment": None,
            "first_comment_status": None,
            "first_comment_attempts": 0,
            "first_comment_error": None,
        }
    return {
        "first_comment": comment,
        "first_comment_status": PENDING,
        "first_comment_attempts": 0,
        "first_comment_error": None,
    }


async def post_and_record(
    *,
    db: Any,
    post: Callable[[str, str], str],
    channel_id: str,
    video_doc: dict[str, Any],
    platform: str,
) -> bool:
    """Post the pending comment on *video_doc* and write the outcome back.

    *post* is the platform service's blocking ``post_comment``; it runs in a
    thread so a slow Graph or Data API call cannot stall the worker loop.
    Returns ``True`` only when the comment actually landed.
    """
    video_id = video_doc["video_id"]
    comment = (video_doc.get("first_comment") or "").strip()
    if not comment:
        return False

    id_field = platform_id_field(platform)
    platform_video_id = video_doc.get(id_field) if id_field else None
    if not platform_video_id:
        logger.warning("[FirstComment] Video '%s' has no %s yet — leaving pending", video_id, id_field)
        return False

    attempts = video_doc.get("first_comment_attempts", 0) + 1

    try:
        comment_id = await asyncio.to_thread(post, platform_video_id, comment)
    except Exception as exc:
        # Deliberately swallowed: the video is already live and a missing
        # comment must not bounce it back or retry the publish. The failure is
        # recorded on the video so the dashboard can show it.
        giving_up = attempts >= MAX_ATTEMPTS
        logger.warning(
            "[FirstComment] Could not comment on '%s' (%s, attempt %d/%d)%s: %s",
            video_id,
            platform,
            attempts,
            MAX_ATTEMPTS,
            " — giving up" if giving_up else "",
            exc,
        )
        await db.videos.update_one(
            {"channel_id": channel_id, "video_id": video_id},
            {
                "$set": {
                    "first_comment_status": FAILED if giving_up else PENDING,
                    "first_comment_attempts": attempts,
                    "first_comment_error": str(exc)[:500],
                    "updated_at": now_ist(),
                }
            },
        )
        return False

    await db.videos.update_one(
        {"channel_id": channel_id, "video_id": video_id},
        {
            "$set": {
                "first_comment_status": POSTED,
                "first_comment_id": comment_id,
                "first_comment_attempts": attempts,
                "first_comment_posted_at": now_ist(),
                "first_comment_error": None,
                "updated_at": now_ist(),
            }
        },
    )
    logger.success("[FirstComment] Commented on '%s' (%s, comment_id=%s)", video_id, platform, comment_id)
    return True
