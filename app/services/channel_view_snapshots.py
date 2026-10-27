"""Store a channel's summed counts at each sync — the readings its hourly views chart is built from.

One small document per channel per sync (``channel_view_snapshots``). Instagram
gives no per-hour account views and YouTube Analytics none at all, so the
difference between two of these is the only per-hour channel number we have.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase


async def record_channel_totals(
    db: AsyncIOMotorDatabase, channel_id: str, platform: str, totals: Mapping[str, float] | None, now: datetime
) -> None:
    """Insert this sync's totals; skipped when they're incomplete, since a short sum would read as a drop."""
    if not totals:
        return
    await db.channel_view_snapshots.insert_one({"channel_id": channel_id, "platform": platform, "at": now, **totals})
