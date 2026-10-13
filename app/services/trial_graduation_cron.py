"""Watches published manual trial reels and emails when one clears its bar.

The timer is state on the video, not a task in memory: ``arm_fields`` stamps
``trial_check_due_at`` when the reel publishes, and every tick asks Mongo what is
due. A restart therefore loses nothing - whatever came due while the server was
down is simply found on the first tick afterwards.
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
from app.services.trial_graduation import FAILED, MET, MISSED, PENDING, evaluate
from app.services.trial_graduation_email import format_graduate_email
from app.timezone import now_ist

logger = get_logger(__name__)

TICK_SECONDS = 300
# A fetch that keeps failing for about an hour is not a blip; stop and say so.
MAX_FETCH_ATTEMPTS = 12
MAX_EMAIL_ATTEMPTS = 5


@dataclass(frozen=True)
class Snapshot:
    permalink: str | None
    metrics: dict[str, float]


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
        return Snapshot(permalink=basics.get("permalink"), metrics={k: float(v) for k, v in meta.items()})

    return fetch


async def _check_one(db: Any, video: dict[str, Any], fetch: FetchSnapshot) -> None:
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
    await db.videos.update_one(
        key,
        {
            "$set": {
                "trial_check_state": MET if outcome.met else MISSED,
                "trial_check_checked_at": now_ist(),
                "trial_check_permalink": snap.permalink,
                "trial_check_result": outcome.model_dump(),
            }
        },
    )


async def _email_met(db: Any, video: dict[str, Any], send: SendEmail) -> None:
    check = video["trial_check"]
    email = format_graduate_email(
        title=video.get("title") or video.get("video_id", "your reel"),
        channel=video.get("channel_id", ""),
        permalink=video.get("trial_check_permalink"),
        after_hours=float(check["after_hours"]),
        match=check.get("match", "all"),
        results=_results(video),
    )
    sent = await send(email.subject, email.text, email.html)
    update: dict[str, Any] = {"trial_check_email_attempts": int(video.get("trial_check_email_attempts", 0)) + 1}
    if sent:
        update["trial_check_emailed"] = True
    await db.videos.update_one({"_id": video["_id"]}, {"$set": update})


def _results(video: dict[str, Any]) -> list[Any]:
    from app.services.trial_graduation import ConditionResult

    stored = (video.get("trial_check_result") or {}).get("results", [])
    return [ConditionResult(**r) for r in stored]


async def run_trial_checks(
    db: Any, fetch: FetchSnapshot, send: SendEmail, now: datetime | None = None, limit: int = 50
) -> dict[str, int]:
    """One pass: evaluate every check that has come due, then send any email still owed."""
    now = now or now_ist()
    due = await db.videos.find({"trial_check_state": PENDING, "trial_check_due_at": {"$lte": now}}).to_list(
        length=limit
    )
    for video in due:
        await _check_one(db, video, fetch)

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
            if stats["checked"] or stats["emails_attempted"]:
                logger.info("Trial graduation tick: %s", stats)
        except Exception as exc:  # noqa: BLE001 - the loop must survive one bad tick
            logger.error("Trial graduation cron tick failed: %s", exc)
            await report_error(feature="Trial graduation cron", message=str(exc), exception=exc)
        await asyncio.sleep(TICK_SECONDS)
