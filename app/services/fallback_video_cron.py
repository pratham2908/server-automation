"""Checks reels that have a fallback video, and publishes the fallback when one misses its bar.

Every tick asks Mongo what is due, like the trial graduation cron: a reel's next
check is ``fallback_due_at`` on its own document. A rule set on a reel that
hasn't published yet has no due time; the first tick after it publishes gives it
one. Each check reads the reel itself (one call), so it doesn't depend on the
channel's auto-sync.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from app.config import get_settings
from app.logger import get_logger
from app.services.error_reporting import report_error
from app.services.fallback_video import (
    FAILED,
    MET,
    PENDING,
    PUBLISH_DELAY,
    TRIGGERED,
    first_due,
    miss_summary,
    next_due,
)
from app.services.schedule_operation import schedule_on_platform
from app.services.trial_graduation import evaluate
from app.services.trial_graduation_cron import FetchSnapshot, SendEmail, email_sender, fetch_snapshot_with
from app.timezone import assume_utc, now_ist, to_ist_iso

logger = get_logger(__name__)

TICK_SECONDS = 300
# A fetch that keeps failing for about an hour is not a blip; stop and say so.
MAX_FETCH_ATTEMPTS = 12
MAX_EMAIL_ATTEMPTS = 5

Publish = Any  # schedule_on_platform's signature; injected so tests don't touch the queues


async def _arm_published(db: Any, now: datetime, limit: int) -> int:
    """Give a first check time to every reel with a waiting rule that has now published."""
    waiting = await db.videos.find(
        {
            "fallback_state": PENDING,
            "fallback_due_at": None,
            "status": "published",
            "published_at": {"$ne": None},
        }
    ).to_list(length=limit)
    for reel in waiting:
        due = max(first_due(reel["published_at"], reel["fallback"]["check"]), now)
        await db.videos.update_one({"_id": reel["_id"]}, {"$set": {"fallback_due_at": due}})
    return len(waiting)


async def _release(db: Any, reel: dict[str, Any]) -> None:
    """Free the fallback video for normal scheduling again."""
    await db.videos.update_one(
        {"channel_id": reel["channel_id"], "video_id": reel["fallback"]["video_id"], "fallback_for": reel["video_id"]},
        {"$set": {"fallback_for": None}},
    )


async def _trigger(db: Any, reel: dict[str, Any], now: datetime, publish: Publish) -> dict[str, Any]:
    """Queue the fallback to publish shortly; the fields to record on the reel."""
    fallback = await db.videos.find_one({"channel_id": reel["channel_id"], "video_id": reel["fallback"]["video_id"]})
    if fallback is None:
        return {"fallback_state": FAILED, "fallback_error": "The fallback video no longer exists"}
    if fallback.get("status") != "ready":
        return {
            "fallback_state": FAILED,
            "fallback_error": f"The fallback video was '{fallback.get('status')}', not ready, so it wasn't published",
        }
    at = now + PUBLISH_DELAY
    result = await publish(
        db=db, platform="instagram", channel_id=reel["channel_id"], video_doc=fallback, scheduled_at=at
    )
    if result.get("status") != "queued":
        reason = result.get("reason") or result.get("status") or "unknown"
        return {"fallback_state": FAILED, "fallback_error": f"Couldn't queue the fallback: {reason}"}
    logger.info("Fallback '%s' queued for %s: '%s' missed its bar", fallback.get("title"), at, reel.get("title"))
    return {"fallback_state": TRIGGERED, "fallback_triggered_at": now, "fallback_publish_at": at}


async def _check_one(db: Any, reel: dict[str, Any], fetch: FetchSnapshot, now: datetime, publish: Publish) -> None:
    key = {"_id": reel["_id"]}
    media_id = reel.get("instagram_media_id")
    if not media_id:
        await db.videos.update_one(
            key, {"$set": {"fallback_state": FAILED, "fallback_error": "The reel has no Instagram media id"}}
        )
        await _release(db, reel)
        return
    try:
        snap = await fetch(reel["channel_id"], media_id)
    except Exception as exc:  # noqa: BLE001 - any fetch failure is retried, then reported
        attempts = int(reel.get("fallback_attempts", 0)) + 1
        logger.warning("Fallback check for %s failed (attempt %d): %s", reel.get("video_id"), attempts, exc)
        update: dict[str, Any] = {"fallback_attempts": attempts}
        if attempts >= MAX_FETCH_ATTEMPTS:
            update.update({"fallback_state": FAILED, "fallback_error": f"Couldn't read the reel's numbers: {exc!s}"})
            await report_error(
                feature="Fallback video check",
                message=f"Gave up reading numbers for '{reel.get('title')}': {exc!s}",
                exception=exc,
                context={"video_id": reel.get("video_id"), "channel_id": reel.get("channel_id")},
            )
        await db.videos.update_one(key, {"$set": update})
        if attempts >= MAX_FETCH_ATTEMPTS:
            await _release(db, reel)
        return

    check = reel["fallback"]["check"]
    outcome = evaluate(check, snap.metrics)
    elapsed = (now - assume_utc(reel["published_at"])).total_seconds() / 3600
    update = {
        "fallback_checked_at": now,
        "fallback_metrics": snap.metrics,
        "fallback_result": outcome.model_dump(),
        "fallback_permalink": snap.permalink,
        "fallback_attempts": 0,
    }
    later = next_due(reel["published_at"], check, now)
    if outcome.met:
        update.update({"fallback_state": MET, "fallback_met_after_hours": round(elapsed, 2)})
        await db.videos.update_one(key, {"$set": update})
        await _release(db, reel)
        return
    if later is not None:
        update["fallback_due_at"] = later
        await db.videos.update_one(key, {"$set": update})
        return
    update.update(await _trigger(db, reel, now, publish))
    await db.videos.update_one(key, {"$set": update})
    if update["fallback_state"] == FAILED:
        await _release(db, reel)


def _email(reel: dict[str, Any], fallback_title: str) -> tuple[str, str]:
    title = reel.get("title") or reel.get("video_id", "your reel")
    numbers = miss_summary(reel.get("fallback_result"))
    if reel.get("fallback_state") == TRIGGERED:
        subject = f"Fallback going out: '{title}' missed its bar"
        body = (
            f"'{title}' missed its bar ({numbers}).\n\n"
            f"Its fallback '{fallback_title}' publishes at {to_ist_iso(reel.get('fallback_publish_at'))}."
        )
    else:
        subject = f"Fallback not published for '{title}'"
        body = f"The fallback check for '{title}' failed: {reel.get('fallback_error')}"
        if numbers:
            body += f"\n\nLast numbers: {numbers}"
    if reel.get("fallback_permalink"):
        body += f"\n\nReel: {reel['fallback_permalink']}"
    return subject, body


async def _email_outcome(db: Any, reel: dict[str, Any], send: SendEmail) -> None:
    fallback = await db.videos.find_one(
        {"channel_id": reel["channel_id"], "video_id": reel["fallback"]["video_id"]}, {"title": 1}
    )
    subject, body = _email(reel, (fallback or {}).get("title") or reel["fallback"]["video_id"])
    sent = await send(subject, body, "")
    update: dict[str, Any] = {"fallback_email_attempts": int(reel.get("fallback_email_attempts", 0)) + 1}
    if sent:
        update["fallback_emailed"] = True
    await db.videos.update_one({"_id": reel["_id"]}, {"$set": update})


async def run_fallback_checks(
    db: Any,
    fetch: FetchSnapshot,
    send: SendEmail,
    now: datetime | None = None,
    publish: Publish = schedule_on_platform,
    limit: int = 50,
) -> dict[str, int]:
    """One pass: arm newly published reels, check what is due, then email any outcome still owed."""
    now = now or now_ist()
    armed = await _arm_published(db, now, limit)
    due = await db.videos.find({"fallback_state": PENDING, "fallback_due_at": {"$ne": None, "$lte": now}}).to_list(
        length=limit
    )
    for reel in due:
        await _check_one(db, reel, fetch, now, publish)
    owed = await db.videos.find(
        {
            "fallback_state": {"$in": [TRIGGERED, FAILED]},
            "fallback_emailed": {"$ne": True},
            "fallback_email_attempts": {"$not": {"$gte": MAX_EMAIL_ATTEMPTS}},
        }
    ).to_list(length=limit)
    for reel in owed:
        await _email_outcome(db, reel, send)
    return {"armed": armed, "checked": len(due), "emails_attempted": len(owed)}


async def run_fallback_video_cron(db: Any, instagram_service_manager: Any) -> None:
    """Loop forever; every tick asks Mongo what is due, so a restart resumes where it left off."""
    logger.info("Fallback video cron started (every %ds)", TICK_SECONDS)
    fetch = fetch_snapshot_with(instagram_service_manager)
    send = email_sender(db, get_settings())
    while True:
        try:
            stats = await run_fallback_checks(db, fetch, send)
            if any(stats.values()):
                logger.info("Fallback video tick: %s", stats)
        except Exception as exc:  # noqa: BLE001 - the loop must survive one bad tick
            logger.error("Fallback video cron tick failed: %s", exc)
            await report_error(feature="Fallback video cron", message=str(exc), exception=exc)
        await asyncio.sleep(TICK_SECONDS)
