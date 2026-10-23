"""Background publisher for Threads video posts.

Threads, like Instagram, has no native schedule: a scheduled video sits in
``schedule_queue`` (``platform: "threads"``) until its time, and this loop posts
it. It mirrors the Instagram auto-publisher — same queue, same retry budget, same
bounce-back marker and failure email — so a linked Threads channel behaves like
any other linked channel.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.database import is_channel_paused, update_channel_task_status
from app.logger import get_logger
from app.services.errors import get_error_service
from app.services.first_comment import post_and_record
from app.services.publish_failure import build_failure_marker
from app.services.publish_failure_notify import notify_publish_failure
from app.services.threads import ThreadsService, ThreadsServiceManager
from app.services.threads_text import threads_text, topic_tag
from app.timezone import now_ist

logger = get_logger(__name__)

_POLL_INTERVAL_SECONDS = 300
_MAX_PUBLISH_ATTEMPTS = 5
# Meta fetches the video while processing the container, which can take minutes.
_VIDEO_URL_TTL_SECONDS = 3 * 3600


async def _bounce(db: Any, entry: dict[str, Any], video_doc: dict[str, Any], exc: BaseException) -> None:
    """Give up: back to ready with a failure marker, and tell the owner."""
    await db.schedule_queue.delete_one({"_id": entry["_id"]})
    marker = build_failure_marker(
        stage="publish", platform="threads", attempts=entry.get("attempt_count", 0) + 1, exc=exc
    )
    await db.videos.update_one(
        {"channel_id": video_doc["channel_id"], "video_id": video_doc["video_id"]},
        {"$set": {"status": "ready", "updated_at": now_ist(), "last_failure": marker}},
    )
    await notify_publish_failure(db, video_doc, marker, entry.get("scheduled_at"))


async def publish_one(
    *,
    db: Any,
    r2_service: Any,
    service: ThreadsService,
    channel_doc: dict[str, Any],
    video_doc: dict[str, Any],
    entry: dict[str, Any],
) -> bool:
    """Post one queued video to Threads. Returns whether it went live."""
    channel_id = channel_doc["channel_id"]
    video_id = video_doc["video_id"]
    r2_key = video_doc.get("r2_object_key")
    try:
        if not r2_key or not r2_service.file_exists(r2_key):
            # Permanent: retrying can't conjure the file, so fail once.
            raise FileNotFoundError(f"Source file missing in storage for video '{video_id}'")
        video_url = r2_service.generate_presigned_url(r2_key, expires_in=_VIDEO_URL_TTL_SECONDS)
        # publish_video polls the container with time.sleep for minutes; inline it would freeze the event loop.
        media_id = await asyncio.to_thread(
            service.publish_video, video_url, threads_text(video_doc), topic_tag(video_doc)
        )
        permalink = await asyncio.to_thread(service.get_permalink, media_id)
    except Exception as exc:
        attempts = entry.get("attempt_count", 0) + 1
        permanent = isinstance(exc, FileNotFoundError)
        logger.exception(
            "[Threads] Failed for video '%s' on channel '%s' (attempt %d/%d)",
            video_id,
            channel_id,
            attempts,
            _MAX_PUBLISH_ATTEMPTS,
        )
        await get_error_service(db).log_error(
            feature="Threads Publisher",
            message=f"Failed to post '{video_doc.get('title', video_id)}' (attempt {attempts}/{_MAX_PUBLISH_ATTEMPTS})",
            exception=exc,
            context={"video_id": video_id, "channel_id": channel_id, "attempt": attempts},
        )
        if permanent or attempts >= _MAX_PUBLISH_ATTEMPTS:
            await _bounce(db, entry, video_doc, exc)
        else:
            await db.schedule_queue.update_one({"_id": entry["_id"]}, {"$set": {"attempt_count": attempts}})
        return False

    now = now_ist()
    await db.videos.update_one(
        {"channel_id": channel_id, "video_id": video_id},
        {
            "$set": {
                "threads_media_id": media_id,
                "threads_permalink": permalink,
                "status": "published",
                "published_at": now,
                "updated_at": now,
            },
            "$unset": {"last_failure": ""},
        },
    )
    await db.schedule_queue.delete_one({"_id": entry["_id"]})

    # The post is live either way: a comment that fails is recorded on the video, never a reason to retry the post.
    if video_doc.get("first_comment"):
        await post_and_record(
            db=db,
            post=service.post_comment,
            channel_id=channel_id,
            video_doc={**video_doc, "threads_media_id": media_id},
            platform="threads",
        )
    logger.success(
        "[Threads] Posted '%s' (media_id=%s) on '%s'", str(video_doc.get("title", video_id))[:50], media_id, channel_id
    )
    await update_channel_task_status(db, channel_id, "threads_publisher")
    return True


async def _poll_and_publish(db: Any, r2_service: Any) -> None:
    now = now_ist()
    due = await db.schedule_queue.find({"platform": "threads", "scheduled_at": {"$lte": now}}).to_list(length=None)
    if not due:
        return
    manager = ThreadsServiceManager(db)
    for entry in due:
        channel_id = entry.get("channel_id", "")
        video_id = entry.get("video_id", "")
        channel_doc = await db.channels.find_one({"channel_id": channel_id})
        if not channel_doc:
            logger.warning("[Threads] Channel '%s' not found — removing queue entry", channel_id)
            await db.schedule_queue.delete_one({"_id": entry["_id"]})
            continue
        if is_channel_paused(channel_doc):
            # Temporary: the post must still be waiting when the channel resumes.
            continue
        video_doc = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
        if not video_doc or video_doc.get("status") not in ("queued", "scheduled"):
            logger.warning("[Threads] Video '%s' on '%s' is gone or not queued — removing entry", video_id, channel_id)
            await db.schedule_queue.delete_one({"_id": entry["_id"]})
            continue
        service = await manager.get_service(channel_id)
        if not service:
            # No token at all can't fix itself in a retry: bounce it so the owner hears about it.
            await _bounce(
                db, entry, video_doc, PermissionError("Threads account not connected (401): reconnect the channel")
            )
            continue
        await publish_one(
            db=db, r2_service=r2_service, service=service, channel_doc=channel_doc, video_doc=video_doc, entry=entry
        )


async def run_threads_publisher(db: Any, r2_service: Any) -> None:
    """Long-running loop that posts due Threads videos every 5 minutes."""
    logger.info("[Threads] Publisher started (poll interval: %ds)", _POLL_INTERVAL_SECONDS)
    while True:
        try:
            await _poll_and_publish(db, r2_service)
        except asyncio.CancelledError:
            logger.info("[Threads] Publisher shutting down")
            break
        except Exception as exc:
            logger.exception("[Threads] Publisher poll failed")
            await get_error_service(db).log_error(
                feature="Threads Publisher Loop", message="Threads publisher poll failed", exception=exc
            )
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
