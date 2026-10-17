"""Keeps the per-reel follow estimate current, as part of each Instagram sync.

The thin side-effect shell around ``instagram_account_insights`` and
``instagram_follow_attribution``. Three steps:

1. Record each reel's cumulative view count (``instagram_reel_snapshots``).
2. Store the account's daily totals (``instagram_account_daily``) for the last
   30 days, which is as far back as Instagram serves them.
3. Recompute every reel's estimated follows, profile visits and bio-link taps onto its video doc,
   with its day-by-day split (``instagram_reel_daily``) and how fast it collected
   its views.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReplaceOne

from app.logger import get_logger
from app.services.instagram import InstagramService
from app.services.instagram_account_insights import AccountDay, parse_account_day
from app.services.instagram_content_analysis import parse_published_at, view_speed
from app.services.instagram_follow_attribution import (
    ReelDay,
    ViewReading,
    daily_attribution,
    estimates_from_timelines,
)
from app.timezone import IST, assume_utc, now_ist

logger = get_logger(__name__)

# Instagram serves account insights for the last 30 days only.
ACCOUNT_HISTORY_DAYS = 30
# Instagram can still be filling in a day for up to 48h; older days are settled.
SETTLED_AFTER_DAYS = 3
# A day older than this is not asked for again even if a call was refused —
# accounts under 100 followers are refused follow data for good.
RETRY_REFUSED_FOR_DAYS = 7
# Bump when an account day gains metrics, so settled days are fetched once more.
ACCOUNT_DAY_VERSION = 2
# A day Instagram is still filling in is re-read at most this often. Syncs run
# hourly while a reel is fresh, but these totals only feed the per-day follow
# split, so once a day is enough (the owner's call) and saves ~10 calls a run.
UNSETTLED_REFRESH_HOURS = 24


async def refresh_follow_estimates(
    db: AsyncIOMotorDatabase,
    ig: InstagramService,
    channel_id: str,
    ig_user_id: str,
    counts_by_media: Mapping[str, Mapping[str, int]],
) -> None:
    now = now_ist()
    if counts_by_media:
        # Views drive the follow estimate; the other counts feed the hourly chart.
        await db.instagram_reel_snapshots.insert_many(
            [
                {"channel_id": channel_id, "media_id": media_id, "at": now, **counts}
                for media_id, counts in counts_by_media.items()
            ]
        )
    await _store_account_days(db, ig, channel_id, ig_user_id, now.date())
    await _write_estimates(db, channel_id)


async def _store_account_days(
    db: AsyncIOMotorDatabase, ig: InstagramService, channel_id: str, ig_user_id: str, today: date
) -> None:
    # A settled day stored by an older version (fewer metrics) is asked for once
    # more, so new metrics cover the full 30 days rather than only new days.
    skip = {
        doc["date"]
        async for doc in db.instagram_account_daily.find(
            {"channel_id": channel_id, "settled": True, "version": {"$gte": ACCOUNT_DAY_VERSION}}, {"date": 1}
        )
    }
    # Unsettled days read in the last day are skipped too; they come round again tomorrow.
    skip |= {
        doc["date"]
        async for doc in db.instagram_account_daily.find(
            {
                "channel_id": channel_id,
                "version": {"$gte": ACCOUNT_DAY_VERSION},
                "fetched_at": {"$gte": now_ist() - timedelta(hours=UNSETTLED_REFRESH_HOURS)},
            },
            {"date": 1},
        )
    }
    # Today is still running, so it starts from yesterday.
    for age in range(1, ACCOUNT_HISTORY_DAYS + 1):
        day = today - timedelta(days=age)
        label = day.isoformat()
        if label in skip:
            continue
        start = datetime(day.year, day.month, day.day, tzinfo=IST)
        until = start + timedelta(days=1)
        totals, views, follows, reach, views_by_follow = await asyncio.to_thread(
            ig.get_account_day_insights, ig_user_id, int(start.timestamp()), int(until.timestamp())
        )
        parsed = parse_account_day(label, totals, views, follows, reach, views_by_follow)
        refused = totals is None or views is None or follows is None
        is_settled = age >= SETTLED_AFTER_DAYS and (not refused or age >= RETRY_REFUSED_FOR_DAYS)
        await db.instagram_account_daily.update_one(
            {"channel_id": channel_id, "date": label},
            {
                "$set": {
                    **asdict(parsed),
                    "channel_id": channel_id,
                    "settled": is_settled,
                    "version": ACCOUNT_DAY_VERSION,
                    "fetched_at": now_ist(),
                }
            },
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
        bio_link_taps=doc.get("bio_link_taps"),
        reach=doc.get("reach"),
        reach_followers=doc.get("reach_followers"),
        reach_non_followers=doc.get("reach_non_followers"),
    )


def _rounded(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _daily_row(channel_id: str, media_id: str, day: ReelDay) -> dict[str, Any]:
    return {
        "channel_id": channel_id,
        "media_id": media_id,
        "date": day.date,
        "views": round(day.views, 1),
        "follows": _rounded(day.follows, 2),
        "profile_visits": _rounded(day.profile_visits, 2),
        "bio_link_taps": _rounded(day.bio_link_taps, 2),
        "rough": day.rough,
    }


async def _write_estimates(db: AsyncIOMotorDatabase, channel_id: str) -> None:
    days = [_account_day(doc) async for doc in db.instagram_account_daily.find({"channel_id": channel_id})]
    readings: dict[str, list[ViewReading]] = {}
    published_by_reel: dict[str, datetime | None] = {}
    async for video in db.videos.find(
        {"channel_id": channel_id, "instagram_media_id": {"$ne": None}},
        {"instagram_media_id": 1, "published_at": 1},
    ):
        published = parse_published_at(video.get("published_at"))
        published_by_reel[video["instagram_media_id"]] = published
        # The publish moment is a free reading of 0 views; it is what lets a reel's
        # first sync place its views on the days since it went up.
        readings[video["instagram_media_id"]] = [ViewReading(at=published, views=0.0)] if published else []
    async for snap in db.instagram_reel_snapshots.find(
        {"channel_id": channel_id}, {"media_id": 1, "at": 1, "views": 1}
    ):
        if snap["media_id"] in readings:
            readings[snap["media_id"]].append(ViewReading(at=assume_utc(snap["at"]), views=float(snap["views"])))

    timelines = daily_attribution(days, readings)
    estimates = estimates_from_timelines(timelines)
    for media_id, reel_readings in readings.items():
        speed = view_speed(published_by_reel.get(media_id), reel_readings)
        set_doc: dict[str, Any] = {
            "metadata.views_24h": _rounded(speed.views_24h, 0),
            "metadata.views_48h": _rounded(speed.views_48h, 0),
            "metadata.views_7d": _rounded(speed.views_7d, 0),
            "metadata.hours_to_half_views": _rounded(speed.hours_to_half),
            "metadata.hours_to_90_views": _rounded(speed.hours_to_90),
            "metadata.picking_up": speed.picking_up,
        }
        est = estimates.get(media_id)
        if est is not None:
            set_doc.update(
                {
                    "metadata.estimated_follows": round(est.follows, 1),
                    "metadata.estimated_profile_visits": round(est.profile_visits, 1),
                    "metadata.estimated_bio_link_taps": round(est.bio_link_taps, 1),
                    "metadata.follow_estimate_quality": est.quality,
                    "metadata.follow_estimate_through": est.through,
                }
            )
        await db.videos.update_one({"channel_id": channel_id, "instagram_media_id": media_id}, {"$set": set_doc})

    # Rows are replaced, not appended: a later reading or a newly reported day
    # changes a day already written.
    rows = [_daily_row(channel_id, media_id, day) for media_id, timeline in timelines.items() for day in timeline]
    if rows:
        await db.instagram_reel_daily.bulk_write(
            [
                ReplaceOne({"channel_id": channel_id, "media_id": r["media_id"], "date": r["date"]}, r, upsert=True)
                for r in rows
            ],
            ordered=False,
        )
