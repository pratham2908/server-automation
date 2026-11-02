from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from app.services.growth_tracking import GrowthTrackingService, velocity_from


def _snapshots(days: int) -> list[dict[str, Any]]:
    start = datetime(2026, 10, 1)
    return [
        {"timestamp": start + timedelta(days=i), "subscribers": 100 + 10 * i, "views": 1000 + 100 * i}
        for i in range(days)
    ]


class _Service(GrowthTrackingService):
    """Reads canned snapshots and counts how often the history is read."""

    def __init__(self, days: int) -> None:
        self.reads: list[int] = []
        self._all = _snapshots(days)

    async def get_history(self, channel_id: str, limit: int = 30) -> list[dict[str, Any]]:
        self.reads.append(limit)
        return self._all[-limit:]

    async def get_milestones(self, channel_id: str) -> list[int]:
        return [500, 100]


def test_overview_reads_the_history_once_and_derives_velocity_from_it() -> None:
    service = _Service(days=40)
    overview = asyncio.run(service.channel_overview("c", limit=30))
    assert service.reads == [31]
    assert len(overview["history"]) == 30
    assert overview["velocity"] == velocity_from(service._all[-31:])
    assert overview["milestones"] == [100, 500]


def test_overview_with_a_long_limit_still_fetches_enough_for_velocity() -> None:
    service = _Service(days=100)
    overview = asyncio.run(service.channel_overview("c", limit=90))
    assert service.reads == [90]
    assert len(overview["history"]) == 90
    assert overview["velocity"]["period_7d"]["subs_total"] == 70


def test_velocity_of_a_single_snapshot_is_zero() -> None:
    assert velocity_from(_snapshots(1))["period_7d"] == {"subs": 0, "views": 0}
