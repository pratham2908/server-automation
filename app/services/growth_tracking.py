from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.logger import get_logger
from app.timezone import now_ist

logger = get_logger(__name__)

MILESTONES = [
    100, 500, 1000, 5000, 10000, 25000, 50000, 100000, 250000, 500000, 1000000, 5000000, 10000000
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
        metadata: Optional[Dict] = None
    ) -> Dict:
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
            "metadata": metadata or {}
        }
        
        # Use update_one with upsert=True to ensure only one snapshot per day
        await self.db.growth_snapshots.update_one(
            {"channel_id": channel_id, "snapshot_date": snapshot_date},
            {"$set": snapshot},
            upsert=True
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
                {"channel_id": channel_id},
                {"$push": {"milestones": {"$each": new_hits}}}
            )
            for m in new_hits:
                logger.success(f"🏆 Milestone Hit! Channel {channel_id} reached {m} subscribers on {platform}")

    async def get_history(self, channel_id: str, limit: int = 30) -> List[Dict]:
        """Fetch historical snapshots for a channel."""
        cursor = self.db.growth_snapshots.find(
            {"channel_id": channel_id}
        ).sort("snapshot_date", -1).limit(limit)
        
        history = await cursor.to_list(length=limit)
        # Return in chronological order
        return history[::-1]

    async def calculate_velocity(self, channel_id: str) -> Dict[str, Any]:
        """Calculate growth velocity (7d and 30d averages)."""
        # Get snapshots for last 31 days to have deltas
        snapshots = await self.get_history(channel_id, limit=31)
        if len(snapshots) < 2:
            return {
                "daily_subs": 0,
                "daily_views": 0,
                "period_7d": {"subs": 0, "views": 0},
                "period_30d": {"subs": 0, "views": 0}
            }
            
        def get_delta(days: int):
            if len(snapshots) < days + 1:
                period_snaps = snapshots
            else:
                period_snaps = snapshots[-(days+1):]
                
            start = period_snaps[0]
            end = period_snaps[-1]
            
            days_elapsed = (end["timestamp"] - start["timestamp"]).days or 1
            
            return {
                "subs_total": end["subscribers"] - start["subscribers"],
                "views_total": end["views"] - start["views"],
                "subs_avg": round((end["subscribers"] - start["subscribers"]) / days_elapsed, 2),
                "views_avg": round((end["views"] - start["views"]) / days_elapsed, 2)
            }

        return {
            "period_7d": get_delta(7),
            "period_30d": get_delta(30)
        }
        
    async def get_milestones(self, channel_id: str) -> List[int]:
        """Get list of milestones hit by the channel."""
        channel = await self.db.channels.find_one({"channel_id": channel_id}, {"milestones": 1})
        return (channel or {}).get("milestones", [])
