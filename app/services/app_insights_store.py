"""Keeps what a screen recording of the Instagram app showed for each reel (see ``app_insights``).

- ``reel_app_insights``: one row per reel per recording, so repeated recordings build a history.
- ``videos.metadata.app_insights``: the newest value of each field across those rows (``latest_view``),
  on the reel itself, since one recording may cover only some tabs.
- ``videos.metadata.follows``: the app's follow count, which is exact and newer than a Business Suite
  paste; a lifetime count only grows, so an older recording imported later never lowers it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.app_insights import DueReel, ReelInsights, due_for_recording, latest_view, match_reel

FOLLOWS_SOURCE = "instagram_app"


@dataclass
class StoreResult:
    stored: list[tuple[str, str]] = field(default_factory=list)  # (media id, title)
    unmatched: list[ReelInsights] = field(default_factory=list)


async def channel_reels(db: AsyncIOMotorDatabase, channel_id: str) -> list[dict[str, Any]]:
    """The channel's reels as ``match_reel`` reads them."""
    out = []
    async for video in db.videos.find(
        {"channel_id": channel_id, "instagram_media_id": {"$nin": [None, ""]}},
        {"instagram_media_id": 1, "title": 1, "metadata.views": 1, "metadata.likes": 1, "metadata.follows": 1},
    ):
        meta = video.get("metadata") or {}
        out.append(
            {
                "media_id": str(video["instagram_media_id"]),
                "title": video.get("title"),
                "views": meta.get("views"),
                "likes": meta.get("likes"),
                "follows": meta.get("follows"),
            }
        )
    return out


async def store(
    db: AsyncIOMotorDatabase, channel_id: str, records: Sequence[ReelInsights], captured_at: datetime, source: str
) -> StoreResult:
    reels = await channel_reels(db, channel_id)
    by_media = {r["media_id"]: r for r in reels}
    result = StoreResult()
    for rec in records:
        media_id = match_reel(rec, reels)
        if media_id is None:
            result.unmatched.append(rec)
            continue
        doc = {**rec.as_doc(), "captured_at": captured_at, "source": source}
        await db.reel_app_insights.update_one(
            {"channel_id": channel_id, "media_id": media_id, "captured_at": captured_at},
            {"$set": doc},
            upsert=True,
        )
        rows = (
            await db.reel_app_insights.find(
                {"channel_id": channel_id, "media_id": media_id}, {"_id": 0, "channel_id": 0, "media_id": 0}
            )
            .sort("captured_at", 1)
            .to_list(None)
        )
        update: dict[str, Any] = {"metadata.app_insights": latest_view(rows)}
        follows = rec.counts.get("follows")
        held = by_media[media_id].get("follows")
        if follows is not None and (not isinstance(held, int) or follows >= held):
            update |= {
                "metadata.follows": follows,
                "metadata.follows_source": FOLLOWS_SOURCE,
                "metadata.follows_as_of": captured_at.date().isoformat(),
            }
        await db.videos.update_one({"channel_id": channel_id, "instagram_media_id": media_id}, {"$set": update})
        result.stored.append((media_id, str(by_media[media_id].get("title") or "")))
    return result


async def due_reels(
    db: AsyncIOMotorDatabase, channel_id: str, threshold: float = 0.20, min_views: int = 100_000
) -> list[DueReel]:
    """``due_for_recording`` over the channel's stored reels."""
    reels = []
    async for video in db.videos.find(
        {"channel_id": channel_id, "instagram_media_id": {"$nin": [None, ""]}},
        {"instagram_media_id": 1, "title": 1, "metadata.views": 1, "metadata.app_insights": 1},
    ):
        meta = video.get("metadata") or {}
        insights = meta.get("app_insights") or {}
        reels.append(
            {
                "media_id": video["instagram_media_id"],
                "title": video.get("title"),
                "views": meta.get("views"),
                "recorded_views": (insights.get("counts") or {}).get("views"),
                # The view count's own date: a later pass may have skipped the Overview tab.
                "recorded_at": (insights.get("as_of") or {}).get("views") or insights.get("captured_at"),
            }
        )
    return due_for_recording(reels, threshold, min_views)
