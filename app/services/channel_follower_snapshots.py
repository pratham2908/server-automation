"""Store a channel's follower (subscriber) count at each sync, so the growth chart can read it by the hour.

``growth_snapshots`` keeps one row per day, the last reading of it, which cannot say *when* in a day
followers moved. These small rows, one per sync, can: the difference between two of them is what the
hourly and quarter-hourly follower charts show. They are kept for ``RETENTION_DAYS``.
"""

from __future__ import annotations

from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

COLLECTION = "channel_follower_snapshots"
RETENTION_DAYS = 120


async def record_follower_reading(db: AsyncIOMotorDatabase, channel_id: str, followers: int, now: datetime) -> None:
    await db[COLLECTION].insert_one({"channel_id": channel_id, "at": now, "followers": int(followers)})
