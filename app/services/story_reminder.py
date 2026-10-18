"""Emails the owner once more when a reel's story is still unticked after the reel is live.

Runs on the trial graduation cron's tick. State is on the video (``story_reminded_at``), so a restart
neither loses a reminder nor sends it twice.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from app.logger import get_logger
from app.services import story_kit as kit

logger = get_logger(__name__)

SendEmail = Callable[[str, str, str], Awaitable[bool]]


async def send_story_reminders(db: Any, send: SendEmail, now: datetime | None = None) -> int:
    """Send one reminder per channel per tick for every due, unticked story. Returns reels reminded."""
    from app.routers.story_kit import channel_page_url

    now = now or datetime.now(timezone.utc)
    videos = await db.videos.find(
        {
            "story_slug": {"$exists": True, "$nin": [None, ""]},
            "story_scheduled_at": {"$exists": False},
            "story_reminded_at": {"$exists": False},
            "scheduled_at": {"$gte": now - kit.KIT_LIFETIME - kit.GRADUATED_REMINDER_DELAY},
        }
    ).to_list(length=200)
    by_channel: dict[str, list[dict[str, Any]]] = {}
    for v in kit.reminders_due(videos, now):
        by_channel.setdefault(v.get("channel_id", ""), []).append(v)

    reminded = 0
    for channel_id, due in by_channel.items():
        items = [
            kit.KitItem(v["video_id"], v["story_slug"], v.get("title") or v["story_slug"], kit.reel_time_ist(v["scheduled_at"]))
            for v in due
        ]
        subject, text, html = kit.kit_email(items, await channel_page_url(db, channel_id), reminder=True)
        if not await send(subject, text, html):
            logger.warning("Story reminder for %s not sent; will retry next tick", channel_id)
            continue
        for v in due:
            await db.videos.update_one({"_id": v["_id"]}, {"$set": {"story_reminded_at": now}})
        reminded += len(due)
    return reminded
