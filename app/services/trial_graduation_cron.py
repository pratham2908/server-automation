"""Watches published manual trial reels and emails when one clears its bar.

Each reel is checked every hour until its "check until" time. The next check is
state on the video, not a task in memory: ``arm_fields`` stamps
``trial_check_due_at`` when the reel publishes, each check moves it an hour on,
and every tick asks Mongo what is due. A restart therefore loses nothing -
whatever came due while the server was down is simply found on the first tick
afterwards.

Each check reads the reel itself (one call) rather than waiting on the channel's
auto-sync: auto-sync is opt-in per channel, and a trial reel on a channel
without it must still be checked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.config import Settings, get_settings
from app.logger import get_logger
from app.services.email_service import resolve_owner_email, send_email
from app.services.error_reporting import report_error
from app.services.instagram_reel_metrics import reel_metadata
from app.services.trial_graduation import (
    FAILED,
    MET,
    MISSED,
    PENDING,
    ConditionResult,
    GroupResult,
    check_until_hours,
    evaluate,
    next_check_at,
)
from app.services.story_reminder import send_story_reminders
from app.services.trial_graduation_email import format_graduate_email
from app.timezone import assume_utc, now_ist

logger = get_logger(__name__)

TICK_SECONDS = 300
# A fetch that keeps failing for about an hour is not a blip; stop and say so.
MAX_FETCH_ATTEMPTS = 12
MAX_EMAIL_ATTEMPTS = 5


@dataclass(frozen=True)
class Snapshot:
    permalink: str | None
    metrics: dict[str, float]
    thumbnail_url: str | None = None


FetchSnapshot = Callable[[str, str], Awaitable[Snapshot]]
SendEmail = Callable[[str, str, str], Awaitable[bool]]


def fetch_snapshot_with(instagram_service_manager: Any) -> FetchSnapshot:
    """A fetcher that reads a reel's link and numbers through the channel's own token."""

    async def fetch(channel_id: str, media_id: str) -> Snapshot:
        svc = await instagram_service_manager.get_service(channel_id)
        if svc is None:
            raise RuntimeError(f"Channel '{channel_id}' has no usable Instagram token")
        basics = await asyncio.to_thread(svc.get_media_snapshot, media_id)
        insights = (await asyncio.to_thread(svc.get_reel_insights, [media_id])).get(media_id, {})
        meta = reel_metadata(basics.get("like_count"), basics.get("comments_count"), insights)
        return Snapshot(
            permalink=basics.get("permalink"),
            metrics={k: float(v) for k, v in meta.items()},
            thumbnail_url=basics.get("thumbnail_url"),
        )

    return fetch


async def _check_one(db: Any, video: dict[str, Any], fetch: FetchSnapshot, now: datetime) -> None:
    key = {"_id": video["_id"]}
    media_id = video.get("instagram_media_id")
    if not media_id:
        await db.videos.update_one(
            key, {"$set": {"trial_check_state": FAILED, "trial_check_error": "The reel has no Instagram media id"}}
        )
        return
    try:
        snap = await fetch(video["channel_id"], media_id)
    except Exception as exc:  # noqa: BLE001 - any fetch failure is retried, then reported
        attempts = int(video.get("trial_check_attempts", 0)) + 1
        logger.warning("Trial check for %s failed (attempt %d): %s", video.get("video_id"), attempts, exc)
        update: dict[str, Any] = {"trial_check_attempts": attempts}
        if attempts >= MAX_FETCH_ATTEMPTS:
            update.update({"trial_check_state": FAILED, "trial_check_error": str(exc)})
            await report_error(
                feature="Trial reel graduation check",
                message=f"Gave up reading numbers for '{video.get('title')}': {exc!s}",
                exception=exc,
                context={"video_id": video.get("video_id"), "channel_id": video.get("channel_id")},
            )
        await db.videos.update_one(key, {"$set": update})
        return

    outcome = evaluate(video["trial_check"], snap.metrics)
    later = None if outcome.met else next_check_at(video, now)
    update: dict[str, Any] = {
        "trial_check_checked_at": now,
        "trial_check_permalink": snap.permalink,
        # Fresh from Instagram at check time: the stored thumbnail is a presigned link that may have lapsed.
        "trial_check_thumbnail_url": snap.thumbnail_url,
        "trial_check_metrics": snap.metrics,
        "trial_check_result": outcome.model_dump(),
        "trial_check_attempts": 0,
    }
    if outcome.met:
        elapsed = (now - assume_utc(video["published_at"])).total_seconds() / 3600
        update.update({"trial_check_state": MET, "trial_check_met_after_hours": round(elapsed, 2)})
    elif later is not None:
        # Short of the bar, but the window is still open: check again in an hour.
        update["trial_check_due_at"] = later
    else:
        update["trial_check_state"] = MISSED
    await db.videos.update_one(key, {"$set": update})


async def _story_card(db: Any, video: dict[str, Any]) -> tuple[str, str]:
    """The story card for a graduated trial, or two empty strings. A failure here must not stop the email."""
    slug = video.get("story_slug")
    if not slug or not video.get("scheduled_at"):
        return "", ""
    try:
        from app.routers.story_kit import channel_page_url
        from app.services import story_kit as kit

        # The channel's one story page lists a graduated trial from now on; the anchor jumps to its card.
        page = await channel_page_url(db, video.get("channel_id", ""))
        url, when = f"{page}#card-{video.get('video_id', '')}", "as soon as you graduate it"
        return kit.story_card_html(url, when), kit.story_card_text(url, when)
    except Exception:  # noqa: BLE001 - the story card is a bonus; the graduation nudge matters more
        logger.exception("Story card for %s failed; sending the graduation email without it", video.get("video_id"))
        return "", ""


async def _email_met(db: Any, video: dict[str, Any], send: SendEmail) -> None:
    check = video["trial_check"]
    story_html, story_text = await _story_card(db, video)
    email = format_graduate_email(
        title=video.get("title") or video.get("video_id", "your reel"),
        channel=video.get("channel_id", ""),
        permalink=video.get("trial_check_permalink"),
        after_hours=float(video.get("trial_check_met_after_hours") or check_until_hours(check)),
        groups=_groups(video),
        thumbnail_url=video.get("trial_check_thumbnail_url") or video.get("thumbnail_url"),
        metrics=video.get("trial_check_metrics") or {},
        story_html=story_html,
        story_text=story_text,
    )
    sent = await send(email.subject, email.text, email.html)
    update: dict[str, Any] = {"trial_check_email_attempts": int(video.get("trial_check_email_attempts", 0)) + 1}
    if sent:
        update["trial_check_emailed"] = True
    await db.videos.update_one({"_id": video["_id"]}, {"$set": update})


def _groups(video: dict[str, Any]) -> list[GroupResult]:
    result = video.get("trial_check_result") or {}
    if "groups" in result:
        return [GroupResult(**g) for g in result["groups"]]
    # A result stored before groups existed: one flat list of "at least" conditions.
    legacy = [
        ConditionResult(metric=r["metric"], op="gte", value=r["minimum"], actual=r.get("actual"), passed=r["passed"])
        for r in result.get("results", [])
    ]
    return [GroupResult(passed=bool(result.get("met")), results=legacy)] if legacy else []


async def run_trial_checks(
    db: Any, fetch: FetchSnapshot, send: SendEmail, now: datetime | None = None, limit: int = 50
) -> dict[str, int]:
    """One pass: evaluate every check that has come due, then send any email still owed."""
    now = now or now_ist()
    due = await db.videos.find({"trial_check_state": PENDING, "trial_check_due_at": {"$lte": now}}).to_list(
        length=limit
    )
    for video in due:
        await _check_one(db, video, fetch, now)

    owed = await db.videos.find(
        {
            "trial_check_state": MET,
            "trial_check_emailed": {"$ne": True},
            "trial_check_email_attempts": {"$not": {"$gte": MAX_EMAIL_ATTEMPTS}},
        }
    ).to_list(length=limit)
    for video in owed:
        await _email_met(db, video, send)
    return {"checked": len(due), "emails_attempted": len(owed)}


def email_sender(db: Any, settings: Settings) -> SendEmail:
    async def send(subject: str, text: str, html: str) -> bool:
        return await send_email(settings, await resolve_owner_email(db, settings), subject, text, html)

    return send


async def run_trial_graduation_cron(db: Any, instagram_service_manager: Any) -> None:
    """Loop forever; every tick asks Mongo what is due, so a restart resumes where it left off."""
    logger.info("Trial graduation cron started (every %ds)", TICK_SECONDS)
    fetch = fetch_snapshot_with(instagram_service_manager)
    send = email_sender(db, get_settings())
    while True:
        try:
            stats = await run_trial_checks(db, fetch, send)
            stats["story_reminders"] = await send_story_reminders(db, send)
            if any(stats.values()):
                logger.info("Trial graduation tick: %s", stats)
        except Exception as exc:  # noqa: BLE001 - the loop must survive one bad tick
            logger.error("Trial graduation cron tick failed: %s", exc)
            await report_error(feature="Trial graduation cron", message=str(exc), exception=exc)
        await asyncio.sleep(TICK_SECONDS)
