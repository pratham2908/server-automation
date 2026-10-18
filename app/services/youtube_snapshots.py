"""Record a YouTube channel's live view, like and comment counts after each sync.

The Data API's counts move within minutes, so these readings are what the
hourly chart and the view-speed numbers (views in the first 24h, 48h, 7 days)
are built from — the same way as Instagram's reel snapshots. Videos in their
first week are read every sync; older ones about once a day.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateOne

from app.services.auto_sync_schedule import needs_reel_insights
from app.services.instagram_content_analysis import view_speed
from app.services.instagram_follow_attribution import ViewReading
from app.timezone import assume_utc

# View speed keeps updating until a video has had its week, plus slack for a
# channel that has dropped to daily syncs.
SPEED_WINDOW_DAYS = 10


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


async def record_youtube_snapshots(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    fetched: list[dict[str, Any]],
    incremental: bool,
    now: datetime,
) -> None:
    """Store a reading for each live video that is due one, then refresh young videos' view speed."""
    last_read = {
        doc["youtube_video_id"]: doc.get("youtube_snapshot_at")
        async for doc in db.videos.find(
            {"channel_id": channel_id, "youtube_video_id": {"$ne": None}},
            {"youtube_video_id": 1, "youtube_snapshot_at": 1},
        )
    }
    due = [
        v
        for v in fetched
        # Private and scheduled videos have nobody watching yet.
        if v.get("youtube_privacy_status") != "private"
        and v.get("published_at") is not None
        and v["youtube_video_id"] in last_read
        and (not incremental or needs_reel_insights(v["published_at"], last_read[v["youtube_video_id"]], now))
    ]
    if not due:
        return
    await db.youtube_video_snapshots.insert_many(
        [
            {
                "channel_id": channel_id,
                "youtube_video_id": v["youtube_video_id"],
                "at": now,
                "views": v["views"],
                "likes": v["likes"],
                "comments": v["comments"],
            }
            for v in due
        ]
    )
    await db.videos.bulk_write(
        [
            UpdateOne(
                {"channel_id": channel_id, "youtube_video_id": v["youtube_video_id"]},
                {"$set": {"youtube_snapshot_at": now}},
            )
            for v in due
        ],
        ordered=False,
    )
    young = [v for v in due if now - assume_utc(v["published_at"]) <= timedelta(days=SPEED_WINDOW_DAYS)]
    for v in young:
        await _write_view_speed(db, channel_id, v["youtube_video_id"], assume_utc(v["published_at"]))


async def _write_view_speed(db: AsyncIOMotorDatabase, channel_id: str, video_id: str, published: datetime) -> None:
    readings = [ViewReading(at=published, views=0.0)]
    async for snap in db.youtube_video_snapshots.find(
        {"channel_id": channel_id, "youtube_video_id": video_id}, {"at": 1, "views": 1}
    ):
        readings.append(ViewReading(at=assume_utc(snap["at"]), views=float(snap["views"])))
    speed = view_speed(published, readings)
    await db.videos.update_one(
        {"channel_id": channel_id, "youtube_video_id": video_id},
        {
            "$set": {
                "metadata.views_24h": _round(speed.views_24h, 0),
                "metadata.views_48h": _round(speed.views_48h, 0),
                "metadata.views_7d": _round(speed.views_7d, 0),
                "metadata.hours_to_half_views": _round(speed.hours_to_half),
                "metadata.hours_to_90_views": _round(speed.hours_to_90),
                "metadata.picking_up": speed.picking_up,
            }
        },
    )
