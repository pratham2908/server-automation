"""Story emails that follow the reel going live: the "post this story now" prompt, then one reminder.

Runs on the trial graduation cron's tick. State is on the video (``story_live_emailed_at``,
``story_reminded_at``), so a restart neither loses an email nor sends it twice.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from app.logger import get_logger
from app.services import story_kit as kit
from app.timezone import assume_utc

logger = get_logger(__name__)

SendEmail = Callable[[str, str, str], Awaitable[bool]]


async def send_story_emails(db: Any, send: SendEmail, now: datetime | None = None) -> int:
    """Send the go-live prompt, then any reminder that has come due. Returns emails' worth of reels."""
    now = now or datetime.now(timezone.utc)
    sent = await _send_due(db, send, now, kit.go_live_due, "story_live_emailed_at", "live")
    sent += await _send_due(db, send, now, kit.flip_due, "story_flip_emailed_at", "graduated")
    return sent + await _send_due(db, send, now, kit.reminders_due, "story_reminded_at", "reminder")


async def _send_due(
    db: Any,
    send: SendEmail,
    now: datetime,
    pick: Callable[[list[dict[str, Any]], datetime], list[dict[str, Any]]],
    stamp: str,
    mode: str,
) -> int:
    """One email per channel for every video ``pick`` finds due, then stamp them. Returns reels emailed."""
    from app.routers.story_kit import channel_page_url

    videos = await db.videos.find(
        {
            "story_slug": {"$exists": True, "$nin": [None, ""]},
            "story_scheduled_at": {"$exists": False},
            stamp: {"$exists": False},
            "status": "published",
            **({"graduated_at": {"$exists": True}} if mode == "graduated" else {}),
            "scheduled_at": {"$gte": now - kit.KIT_LIFETIME - kit.GRADUATED_REMINDER_DELAY},
        }
    ).to_list(length=200)
    by_channel: dict[str, list[dict[str, Any]]] = {}
    for v in pick(videos, now):
        by_channel.setdefault(v.get("channel_id", ""), []).append(v)

    reminded = 0
    for channel_id, due in by_channel.items():
        items = [
            kit.KitItem(
                v["video_id"], v["story_slug"], v.get("title") or v["story_slug"], kit.reel_time_ist(v["scheduled_at"])
            )
            for v in due
        ]
        subject, text, html = kit.kit_email(
            items, await channel_page_url(db, channel_id), reminder=mode == "reminder", graduated=mode == "graduated"
        )
        if not await send(subject, text, html):
            logger.warning("Story email for %s not sent; will retry next tick", channel_id)
            continue
        for v in due:
            extra = await _flip_note(db, v) if mode == "graduated" else {}
            await db.videos.update_one({"_id": v["_id"]}, {"$set": {stamp: now, **extra}})
        reminded += len(due)
    return reminded


async def _flip_note(db: Any, v: dict[str, Any]) -> dict[str, Any]:
    """Views per hour just before the grid flip, logged so a self-graduating reel can be compared with ours."""
    media_id = v.get("instagram_media_id")
    if not media_id:
        return {}
    before = (
        await db.instagram_reel_snapshots.find(
            {"channel_id": v.get("channel_id"), "media_id": media_id, "at": {"$lte": v["graduated_at"]}}
        )
        .sort("at", -1)
        .to_list(length=2)
    )
    if len(before) < 2 or before[0].get("views") is None or before[1].get("views") is None:
        return {}
    hours = (assume_utc(before[0]["at"]) - assume_utc(before[1]["at"])).total_seconds() / 3600
    if hours <= 0:
        return {}
    rate = round((before[0]["views"] - before[1]["views"]) / hours)
    logger.info("Trial %s graduated at %s, %d views/h just before", v.get("video_id"), v["graduated_at"], rate)
    return {"graduated_rate_before": rate}
