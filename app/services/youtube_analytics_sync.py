"""Once a day, pull a YouTube channel's Analytics numbers into the database.

YouTube Analytics reports whole days, two to three days behind, so reading it
more than daily buys nothing; the live view, like and comment counts that do
move hour by hour come from the Data API in the sync itself (``youtube_snapshots``).

What one refresh stores:

* every published video's lifetime watch time, average view percentage,
  engaged views (→ swiped-away rate), subscribers gained/lost and shares, on
  the video's ``metadata``;
* a per-day timeline (``youtube_video_daily``) for videos under four weeks old —
  with real subscribers gained per video per day, which Instagram never gives;
* the channel's own per-day totals (``youtube_channel_daily``);
* the last four weeks' audience — age and gender, countries, traffic sources,
  devices, subscribed or not (``youtube_audience``);
* retention curves for a few recent videos at a time (``youtube_retention``).

Each part is read and written on its own: YouTube refusing one report (it
returns a 500 for some dimension mixes) must not cost the others their refresh.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReplaceOne, UpdateOne

from app.logger import get_logger
from app.services.auto_sync_schedule import OLD_REEL_REFRESH_HOURS
from app.services.instagram_content_analysis import parse_published_at
from app.services.youtube import YouTubeService
from app.services.youtube_analytics_parse import (
    CHANNEL_DAILY_METRICS,
    DAILY_METRICS,
    LIFETIME_METRICS,
    age_gender_rows,
    channel_daily_row,
    daily_row,
    lifetime_metadata,
    retention_points,
    row_dicts,
    share_rows,
)
from app.timezone import assume_utc

logger = get_logger(__name__)

# Ids per lifetime report; the filter string has a length limit.
LIFETIME_BATCH = 40
# Videos this young get a per-day timeline.
DAILY_TIMELINE_DAYS = 28
AUDIENCE_DAYS = 28
CHANNEL_HISTORY_DAYS = 90
# Retention needs a few days of views before YouTube reports a curve, and is
# re-read weekly while a video is still collecting them.
RETENTION_MIN_AGE_DAYS = 2
RETENTION_MAX_AGE_DAYS = 60
RETENTION_REFRESH_DAYS = 7
RETENTION_PER_RUN = 10


def analytics_due(channel: dict[str, Any], now: datetime) -> bool:
    last = channel.get("youtube_analytics_synced_at")
    if not isinstance(last, datetime):
        return True
    return now - assume_utc(last) >= timedelta(hours=OLD_REEL_REFRESH_HOURS)


async def _report(yt: YouTubeService, **query: str | int) -> list[dict[str, Any]]:
    headers, rows = await asyncio.to_thread(yt.analytics_report, **query)
    return row_dicts(headers, rows)


async def refresh_youtube_analytics(
    db: AsyncIOMotorDatabase, yt: YouTubeService, channel_id: str, channel: dict[str, Any], now: datetime
) -> None:
    """Pull the day's Analytics numbers when a day has passed since the last pull."""
    if not analytics_due(channel, now):
        return
    videos = await db.videos.find(
        {"channel_id": channel_id, "youtube_video_id": {"$ne": None}, "status": "published"},
        {"youtube_video_id": 1, "published_at": 1, "retention_fetched_at": 1},
    ).to_list(None)
    today = now.date().isoformat()
    parts = (
        ("lifetime", _store_lifetime(db, yt, channel_id, videos, today)),
        ("video days", _store_video_days(db, yt, channel_id, videos, now)),
        ("channel days", _store_channel_days(db, yt, channel_id, now)),
        ("audience", _store_audience(db, yt, channel_id, now)),
        ("retention", _store_retention(db, yt, channel_id, videos, now)),
    )
    for name, part in parts:
        try:
            await part
        except Exception:
            # Logged, not raised: the other parts are independent, and tomorrow's run retries this one.
            logger.exception("YouTube Analytics %s not refreshed for '%s'", name, channel_id)
    await db.channels.update_one({"channel_id": channel_id}, {"$set": {"youtube_analytics_synced_at": now}})


async def _store_lifetime(
    db: AsyncIOMotorDatabase, yt: YouTubeService, channel_id: str, videos: list[dict[str, Any]], today: str
) -> None:
    ids = [v["youtube_video_id"] for v in videos]
    updates = []
    for i in range(0, len(ids), LIFETIME_BATCH):
        batch = ids[i : i + LIFETIME_BATCH]
        rows = await _report(
            yt,
            startDate="2005-01-01",
            endDate=today,
            dimensions="video",
            metrics=",".join(LIFETIME_METRICS),
            filters=f"video=={','.join(batch)}",
            maxResults=200,
        )
        for row in rows:
            meta = lifetime_metadata(row)
            if meta:
                updates.append(
                    UpdateOne(
                        {"channel_id": channel_id, "youtube_video_id": row["video"]},
                        {"$set": {f"metadata.{k}": v for k, v in meta.items()}},
                    )
                )
    if updates:
        await db.videos.bulk_write(updates, ordered=False)


async def _store_video_days(
    db: AsyncIOMotorDatabase, yt: YouTubeService, channel_id: str, videos: list[dict[str, Any]], now: datetime
) -> None:
    for video in videos:
        published = parse_published_at(video.get("published_at"))
        if published is None or now - published > timedelta(days=DAILY_TIMELINE_DAYS):
            continue
        video_id = video["youtube_video_id"]
        rows = await _report(
            yt,
            # YouTube's days are Pacific; starting the day before publishing in UTC covers it.
            startDate=(published - timedelta(days=1)).date().isoformat(),
            endDate=now.date().isoformat(),
            dimensions="day",
            metrics=",".join(DAILY_METRICS),
            filters=f"video=={video_id}",
        )
        docs = [daily_row(channel_id, video_id, row) for row in rows]
        if docs:
            await db.youtube_video_daily.bulk_write(
                [
                    ReplaceOne(
                        {"channel_id": channel_id, "youtube_video_id": video_id, "date": d["date"]}, d, upsert=True
                    )
                    for d in docs
                ],
                ordered=False,
            )


async def _store_channel_days(db: AsyncIOMotorDatabase, yt: YouTubeService, channel_id: str, now: datetime) -> None:
    rows = await _report(
        yt,
        startDate=(now - timedelta(days=CHANNEL_HISTORY_DAYS)).date().isoformat(),
        endDate=now.date().isoformat(),
        dimensions="day",
        metrics=",".join(CHANNEL_DAILY_METRICS),
    )
    docs = [channel_daily_row(channel_id, row) for row in rows]
    if docs:
        await db.youtube_channel_daily.bulk_write(
            [ReplaceOne({"channel_id": channel_id, "date": d["date"]}, d, upsert=True) for d in docs],
            ordered=False,
        )


async def _store_audience(db: AsyncIOMotorDatabase, yt: YouTubeService, channel_id: str, now: datetime) -> None:
    window = {
        "startDate": (now - timedelta(days=AUDIENCE_DAYS)).date().isoformat(),
        "endDate": now.date().isoformat(),
    }
    doc: dict[str, Any] = {"channel_id": channel_id, "fetched_at": now, "period_days": AUDIENCE_DAYS}
    reports: tuple[tuple[str, dict[str, str | int]], ...] = (
        ("age_gender", {"dimensions": "ageGroup,gender", "metrics": "viewerPercentage"}),
        ("countries", {"dimensions": "country", "metrics": "views", "sort": "-views", "maxResults": 25}),
        ("traffic_sources", {"dimensions": "insightTrafficSourceType", "metrics": "views", "sort": "-views"}),
        ("devices", {"dimensions": "deviceType", "metrics": "views", "sort": "-views"}),
        ("subscribed", {"dimensions": "subscribedStatus", "metrics": "views"}),
    )
    dimension_key = {
        "countries": "country",
        "traffic_sources": "insightTrafficSourceType",
        "devices": "deviceType",
        "subscribed": "subscribedStatus",
    }
    for name, query in reports:
        try:
            rows = await _report(yt, **window, **query)
        except Exception as exc:
            # One refused breakdown (YouTube 500s on some for small channels) leaves that
            # list out; the rest still store. Expected often enough that a traceback is noise.
            logger.warning("YouTube audience '%s' not read for '%s': %s", name, channel_id, exc)
            continue
        doc[name] = age_gender_rows(rows) if name == "age_gender" else share_rows(rows, dimension_key[name], "views")
    await db.youtube_audience.replace_one({"channel_id": channel_id}, doc, upsert=True)


async def _store_retention(
    db: AsyncIOMotorDatabase, yt: YouTubeService, channel_id: str, videos: list[dict[str, Any]], now: datetime
) -> None:
    due = []
    for video in videos:
        published = parse_published_at(video.get("published_at"))
        if published is None:
            continue
        age = now - published
        if not timedelta(days=RETENTION_MIN_AGE_DAYS) <= age <= timedelta(days=RETENTION_MAX_AGE_DAYS):
            continue
        fetched = video.get("retention_fetched_at")
        if isinstance(fetched, datetime) and now - assume_utc(fetched) < timedelta(days=RETENTION_REFRESH_DAYS):
            continue
        due.append(video)
    # Newest first: the most recent videos are the ones being judged right now.
    due.sort(key=lambda v: parse_published_at(v.get("published_at")) or now, reverse=True)
    for video in due[:RETENTION_PER_RUN]:
        video_id = video["youtube_video_id"]
        rows = await _report(
            yt,
            startDate="2005-01-01",
            endDate=now.date().isoformat(),
            dimensions="elapsedVideoTimeRatio",
            metrics="audienceWatchRatio,relativeRetentionPerformance",
            filters=f"video=={video_id}",
        )
        # Stored even when empty: YouTube withholds curves for small audiences, and
        # the stamp stops every run re-asking until the week is up.
        await db.youtube_retention.replace_one(
            {"channel_id": channel_id, "youtube_video_id": video_id},
            {
                "channel_id": channel_id,
                "youtube_video_id": video_id,
                "fetched_at": now,
                "points": retention_points(rows),
            },
            upsert=True,
        )
        await db.videos.update_one(
            {"channel_id": channel_id, "youtube_video_id": video_id}, {"$set": {"retention_fetched_at": now}}
        )
