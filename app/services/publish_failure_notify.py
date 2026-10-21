"""Tells the owner by email that a scheduled upload or publish gave up.

Best effort by design: this runs inside the worker's give-up path, which has already
done its real job (bouncing the video back to ready), so nothing here may raise.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.logger import get_logger
from app.services.email_service import resolve_owner_email, send_email
from app.services.publish_failure_email import format_failure_email

logger = get_logger(__name__)


async def notify_publish_failure(db: Any, video: dict[str, Any], marker: dict[str, Any], scheduled_at: Any) -> bool:
    """Email the owner about a video that failed every retry. Returns whether a mail was sent."""
    try:
        settings = get_settings()
        channel = await db.channels.find_one({"channel_id": video.get("channel_id")}, {"name": 1})
        mail = format_failure_email(
            title=video.get("title") or video.get("video_id") or "Untitled",
            channel=(channel or {}).get("name") or str(video.get("channel_id")),
            platform=marker["platform"],
            stage=marker["stage"],
            reason=marker["reason"],
            detail=marker.get("detail") or "",
            attempts=int(marker["attempts"]),
            scheduled_at=scheduled_at,
            app_url=settings.ANALYZER_PUBLIC_URL,
        )
        return await send_email(settings, await resolve_owner_email(db, settings), mail.subject, mail.text, mail.html)
    except Exception:  # noqa: BLE001 - a failed notification must never undo or mask the failure it reports
        logger.exception("Could not send the publish-failure email for video %s", video.get("video_id"))
        return False
