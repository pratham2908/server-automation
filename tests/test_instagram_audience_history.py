"""History shaping for the demographics-over-time chart."""

from __future__ import annotations

from typing import Any

from app.services.instagram_audience_history import history_days


def _doc(date: str, rows: list[tuple[str, int]], audience: str = "reached", frame: str = "this_week") -> dict[str, Any]:
    return {
        "date": date,
        "audiences": {audience: {frame: {"country": [{"value": v, "count": c} for v, c in rows]}}},
    }


def test_orders_oldest_first_and_flags_repeats() -> None:
    docs = [
        _doc("2026-10-18", [("IN", 12), ("US", 4)]),
        _doc("2026-10-16", [("IN", 10), ("US", 4)]),
        _doc("2026-10-17", [("IN", 10), ("US", 4)]),
    ]
    days = history_days(docs, "reached", "this_week", "country")
    assert [d["date"] for d in days] == ["2026-10-16", "2026-10-17", "2026-10-18"]
    assert [d["changed"] for d in days] == [True, False, True]


def test_drops_days_without_the_selection() -> None:
    docs = [_doc("2026-10-16", [("IN", 1)]), {"date": "2026-10-17", "audiences": {}}]
    assert [d["date"] for d in history_days(docs, "reached", "this_week", "country")] == ["2026-10-16"]
    assert history_days(docs, "reached", "last_90_days", "country") == []


def test_followers_ignore_the_requested_timeframe() -> None:
    docs = [_doc("2026-10-16", [("IN", 9)], audience="followers", frame="this_month")]
    assert len(history_days(docs, "followers", "last_90_days", "country")) == 1
