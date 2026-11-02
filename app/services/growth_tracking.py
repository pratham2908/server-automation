from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.timezone import IST, assume_utc, now_ist

logger = get_logger(__name__)

MILESTONES = [
    100,
    500,
    1000,
    5000,
    10000,
    25000,
    50000,
    100000,
    250000,
    500000,
    1000000,
    5000000,
    10000000,
]


class GrowthTrackingService:
    """Handles daily snapshots, growth velocity, and milestone detection for channels."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.db = db

    async def record_snapshot(
        self,
        channel_id: str,
        platform: str,
        subscribers: int,
        views: int,
        metadata: dict | None = None,
    ) -> dict:
        """Record a daily snapshot for a channel."""
        now = now_ist()
        snapshot_date = now.strftime("%Y-%m-%d")

        snapshot = {
            "channel_id": channel_id,
            "platform": platform,
            "snapshot_date": snapshot_date,
            "subscribers": subscribers,
            "views": views,
            "timestamp": now,
            "metadata": metadata or {},
        }

        # Use update_one with upsert=True to ensure only one snapshot per day
        await self.db.growth_snapshots.update_one(
            {"channel_id": channel_id, "snapshot_date": snapshot_date},
            {"$set": snapshot},
            upsert=True,
        )

        logger.info(f"Recorded growth snapshot for {channel_id} (subs: {subscribers}, views: {views})")

        # Check milestones
        await self._check_milestones(channel_id, platform, subscribers)

        return snapshot

    async def _check_milestones(self, channel_id: str, platform: str, current_subs: int) -> None:
        """Detect and record new milestones hit."""
        channel = await self.db.channels.find_one({"channel_id": channel_id}, {"milestones": 1})
        if not channel:
            return

        hit_list = channel.get("milestones", [])
        new_hits = []

        for m in MILESTONES:
            if current_subs >= m and m not in hit_list:
                new_hits.append(m)

        if new_hits:
            await self.db.channels.update_one(
                {"channel_id": channel_id}, {"$push": {"milestones": {"$each": new_hits}}}
            )
            for m in new_hits:
                logger.success(f"🏆 Milestone Hit! Channel {channel_id} reached {m} subscribers on {platform}")

    async def get_history(self, channel_id: str, limit: int = 30) -> list[dict]:
        """Fetch historical snapshots for a channel, oldest first, with view totals filled in where the platform has none."""
        cursor = self.db.growth_snapshots.find({"channel_id": channel_id}).sort("snapshot_date", -1).limit(limit)

        history = await cursor.to_list(length=limit)
        # Return in chronological order
        history = history[::-1]
        if any(not snap.get("views") for snap in history):
            since = _day_end(history[0]["snapshot_date"]) - timedelta(days=2)
            readings = (
                await self.db.channel_view_snapshots.find(
                    {"channel_id": channel_id, "at": {"$gte": since.replace(tzinfo=None)}}, {"at": 1, "views": 1}
                )
                .sort("at", 1)
                .to_list(length=None)
            )
            fill_view_totals(history, readings)
        return history

    async def calculate_velocity(self, channel_id: str) -> dict[str, Any]:
        """Calculate growth velocity (7d and 30d averages)."""
        # Get snapshots for last 31 days to have deltas
        return velocity_from(await self.get_history(channel_id, limit=31))

    async def channel_overview(self, channel_id: str, limit: int) -> dict[str, Any]:
        """History, velocity and milestones for one channel from a single read of its snapshots.

        The three used to be separate requests, and velocity read the same history a second time.
        """
        snapshots = await self.get_history(channel_id, limit=max(limit, 31))
        milestones = await self.get_milestones(channel_id)
        return {
            "history": snapshots[-limit:],
            "velocity": velocity_from(snapshots[-31:]),
            "milestones": sorted(milestones),
        }

    async def get_milestones(self, channel_id: str) -> list[int]:
        """Get list of milestones hit by the channel."""
        channel = await self.db.channels.find_one({"channel_id": channel_id}, {"milestones": 1})
        from typing import cast

        return cast(list[int], (channel or {}).get("milestones", []))


def velocity_from(snapshots: list[dict]) -> dict[str, Any]:
    """7d and 30d growth from chronological snapshots (the newest 31 are enough)."""
    if len(snapshots) < 2:
        return {
            "daily_subs": 0,
            "daily_views": 0,
            "period_7d": {"subs": 0, "views": 0},
            "period_30d": {"subs": 0, "views": 0},
        }

    def get_delta(days: int):
        if len(snapshots) < days + 1:
            period_snaps = snapshots
        else:
            period_snaps = snapshots[-(days + 1) :]

        start = period_snaps[0]
        end = period_snaps[-1]

        days_elapsed = (end["timestamp"] - start["timestamp"]).days or 1

        return {
            "subs_total": end["subscribers"] - start["subscribers"],
            "views_total": end["views"] - start["views"],
            "subs_avg": round((end["subscribers"] - start["subscribers"]) / days_elapsed, 2),
            "views_avg": round((end["views"] - start["views"]) / days_elapsed, 2),
        }

    return {"period_7d": get_delta(7), "period_30d": get_delta(30)}


def _day_end(snapshot_date: str) -> datetime:
    """The end of an IST calendar day, as an aware datetime."""
    return datetime.strptime(snapshot_date, "%Y-%m-%d").replace(tzinfo=IST) + timedelta(days=1)


def fill_view_totals(history: list[dict], readings: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Fill in what an Instagram (or Threads) daily snapshot can't hold by itself.

    Neither platform reports a lifetime account view total, so their snapshots
    store 0 views. Every sync, though, records the channel's summed reel views in
    ``channel_view_snapshots``; a day's total is the last of those readings before
    the day ended. A day before the first reading keeps its 0. The post count is
    stored as ``media_count`` on Instagram, so it is copied to ``video_count``,
    the key the growth chart reads. Mutates and returns *history*.
    """
    points = sorted(
        ((assume_utc(r["at"]), r["views"]) for r in readings if r.get("views") is not None and r.get("at")),
        key=lambda p: p[0],
    )
    for snap in history:
        if not isinstance(snap.get("metadata"), dict):
            snap["metadata"] = {}
        meta = snap["metadata"]
        if "video_count" not in meta and "media_count" in meta:
            meta["video_count"] = meta["media_count"]
        if snap.get("views") or not snap.get("snapshot_date"):
            continue
        end = _day_end(snap["snapshot_date"])
        before = [views for at, views in points if at < end]
        if before:
            snap["views"] = int(before[-1])
    return history
