"""Keeps the per-reel follow estimate current, as part of each Instagram sync.

The thin side-effect shell around ``instagram_account_insights`` and
``instagram_follow_attribution``. Three steps:

1. Record each reel's cumulative view count (``instagram_reel_snapshots``).
2. Store the account's daily totals (``instagram_account_daily``) for the last
   30 days, which is as far back as Instagram serves them.
3. Recompute every reel's estimated follows and profile visits onto its video doc.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.instagram import InstagramService
from app.services.instagram_account_insights import AccountDay, parse_account_day
from app.services.instagram_follow_attribution import ViewReading, estimate_follows
from app.timezone import IST, assume_utc, now_ist

logger = get_logger(__name__)

# Instagram serves account insights for the last 30 days only.
ACCOUNT_HISTORY_DAYS = 30
# Instagram can still be filling in a day for up to 48h; older days are settled.
SETTLED_AFTER_DAYS = 3
# A day older than this is not asked for again even if a call was refused —
# accounts under 100 followers are refused follow data for good.
RETRY_REFUSED_FOR_DAYS = 7


async def refresh_follow_estimates(
    db: AsyncIOMotorDatabase,
    ig: InstagramService,
    channel_id: str,
    ig_user_id: str,
    views_by_media: Mapping[str, int],
) -> None:
    now = now_ist()
    if views_by_media:
        await db.instagram_reel_snapshots.insert_many(
            [
                {"channel_id": channel_id, "media_id": media_id, "at": now, "views": views}
                for media_id, views in views_by_media.items()
            ]
        )
    await _store_account_days(db, ig, channel_id, ig_user_id, now.date())
    await _write_estimates(db, channel_id)


async def _store_account_days(
    db: AsyncIOMotorDatabase, ig: InstagramService, channel_id: str, ig_user_id: str, today: date
) -> None:
    settled = {
        doc["date"]
        async for doc in db.instagram_account_daily.find({"channel_id": channel_id, "settled": True}, {"date": 1})
    }
    # Today is still running, so it starts from yesterday.
    for age in range(1, ACCOUNT_HISTORY_DAYS + 1):
        day = today - timedelta(days=age)
        label = day.isoformat()
        if label in settled:
            continue
        start = datetime(day.year, day.month, day.day, tzinfo=IST)
        until = start + timedelta(days=1)
        totals, views, follows = await asyncio.to_thread(
            ig.get_account_day_insights, ig_user_id, int(start.timestamp()), int(until.timestamp())
        )
        parsed = parse_account_day(label, totals, views, follows)
        refused = totals is None or views is None or follows is None
        is_settled = age >= SETTLED_AFTER_DAYS and (not refused or age >= RETRY_REFUSED_FOR_DAYS)
        await db.instagram_account_daily.update_one(
            {"channel_id": channel_id, "date": label},
            {"$set": {**asdict(parsed), "channel_id": channel_id, "settled": is_settled, "fetched_at": now_ist()}},
            upsert=True,
        )


def _account_day(doc: Mapping[str, Any]) -> AccountDay:
    return AccountDay(
        date=doc["date"],
        profile_visits=doc.get("profile_visits"),
        reel_views=doc.get("reel_views"),
        total_views=doc.get("total_views"),
        follows=doc.get("follows"),
        unfollows=doc.get("unfollows"),
        interactions=doc.get("interactions"),
        accounts_engaged=doc.get("accounts_engaged"),
    )


def _as_datetime(value: object) -> datetime | None:
    """Older video docs hold ``published_at`` as an ISO string, newer ones as a datetime."""
    if isinstance(value, datetime):
        return assume_utc(value)
    if isinstance(value, str) and value:
        try:
            return assume_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


async def _write_estimates(db: AsyncIOMotorDatabase, channel_id: str) -> None:
    days = [_account_day(doc) async for doc in db.instagram_account_daily.find({"channel_id": channel_id})]
    readings: dict[str, list[ViewReading]] = {}
    async for video in db.videos.find(
        {"channel_id": channel_id, "instagram_media_id": {"$ne": None}},
        {"instagram_media_id": 1, "published_at": 1},
    ):
        published = _as_datetime(video.get("published_at"))
        # The publish moment is a free reading of 0 views; it is what lets a reel's
        # first sync place its views on the days since it went up.
        readings[video["instagram_media_id"]] = [ViewReading(at=published, views=0.0)] if published else []
    async for snap in db.instagram_reel_snapshots.find(
        {"channel_id": channel_id}, {"media_id": 1, "at": 1, "views": 1}
    ):
        if snap["media_id"] in readings:
            readings[snap["media_id"]].append(ViewReading(at=assume_utc(snap["at"]), views=float(snap["views"])))

    for media_id, est in estimate_follows(days, readings).items():
        await db.videos.update_one(
            {"channel_id": channel_id, "instagram_media_id": media_id},
            {
                "$set": {
                    "metadata.estimated_follows": round(est.follows, 1),
                    "metadata.estimated_profile_visits": round(est.profile_visits, 1),
                    "metadata.follow_estimate_quality": est.quality,
                    "metadata.follow_estimate_through": est.through,
                }
            },
        )
