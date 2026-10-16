"""Fills in ``metadata.duration_seconds`` on Instagram reels that don't have it yet."""

from __future__ import annotations

import asyncio

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.instagram import InstagramService
from app.services.r2 import R2Service
from app.services.reel_duration import probe_duration_seconds

logger = get_logger(__name__)

# Reels probed per sync. A reel without an R2 copy costs one Graph call for its
# CDN URL, so a channel's back catalogue is filled in over a few syncs rather
# than all at once against the shared app's rate limit.
PROBES_PER_SYNC = 15
URL_TTL_SECONDS = 600


async def refresh_reel_durations(
    db: AsyncIOMotorDatabase, ig: InstagramService, r2: R2Service | None, channel_id: str
) -> int:
    """Probe up to ``PROBES_PER_SYNC`` reels' lengths; returns how many were stored."""
    cursor = db.videos.find(
        {
            "channel_id": channel_id,
            "instagram_media_id": {"$ne": None},
            "status": "published",
            "metadata.duration_seconds": {"$exists": False},
        },
        {"instagram_media_id": 1, "r2_object_key": 1},
    ).limit(PROBES_PER_SYNC)
    stored = 0
    async for video in cursor:
        media_id = video["instagram_media_id"]
        key = video.get("r2_object_key")
        try:
            if key and r2 is not None:
                url = await asyncio.to_thread(r2.generate_presigned_url, key, URL_TTL_SECONDS)
            else:
                url = await asyncio.to_thread(ig.get_reel_media_url, media_id)
        except Exception:
            # One reel's URL failing (deleted mid-sync, a CDN hiccup) must not stop the rest.
            logger.exception("No URL to probe for reel %s on '%s'", media_id, channel_id)
            continue
        seconds = await asyncio.to_thread(probe_duration_seconds, url)
        if seconds is None:
            continue
        await db.videos.update_one({"_id": video["_id"]}, {"$set": {"metadata.duration_seconds": round(seconds, 2)}})
        stored += 1
    return stored
