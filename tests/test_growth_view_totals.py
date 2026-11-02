"""Instagram growth snapshots get their view totals from the per-sync channel readings."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services.growth_tracking import GrowthTrackingService, fill_view_totals
from tests.mongo_fakes import DB


def _snap(date: str, views: int = 0, **meta: int) -> dict:
    return {"channel_id": "z", "snapshot_date": date, "subscribers": 10, "views": views, "metadata": dict(meta)}


# Readings are naive UTC, as Mongo returns them. 31 Oct 20:00 UTC is 1 Nov 01:30 IST.
READINGS = [
    {"at": datetime(2026, 10, 31, 6, 0), "views": 100},
    {"at": datetime(2026, 10, 31, 17, 0), "views": 300},
    {"at": datetime(2026, 10, 31, 20, 0), "views": 450},
    {"at": datetime(2026, 11, 1, 10, 0), "views": 900},
]


def test_a_day_takes_the_last_reading_before_its_ist_midnight():
    history = [_snap("2026-10-31"), _snap("2026-11-01")]
    fill_view_totals(history, READINGS)
    assert [s["views"] for s in history] == [300, 900]


def test_a_day_before_the_first_reading_keeps_zero_and_real_totals_are_untouched():
    history = [_snap("2026-10-20"), _snap("2026-10-31", views=7)]
    fill_view_totals(history, READINGS)
    assert [s["views"] for s in history] == [0, 7]


def test_the_instagram_post_count_reaches_the_key_the_chart_reads():
    history = [_snap("2026-11-01", media_count=11), _snap("2026-11-01", video_count=4, media_count=9)]
    fill_view_totals(history, [])
    assert [s["metadata"]["video_count"] for s in history] == [11, 4]


@pytest.mark.asyncio
async def test_history_comes_back_with_the_filled_totals():
    db = DB(
        growth_snapshots=[_snap("2026-10-31", media_count=10), _snap("2026-11-01", media_count=12)],
        channel_view_snapshots=[{"channel_id": "z", **r} for r in READINGS],
    )
    service = GrowthTrackingService(db)
    history = await service.get_history("z")
    assert [(s["views"], s["metadata"]["video_count"]) for s in history] == [(300, 10), (900, 12)]
