"""The actual-retention backfill must not claim a curve it does not have.

``actual_retention_curve`` used to be written unconditionally, so a video whose
platform returned no curve got ``{}``. The field was then present, and the
prediction tab reads presence as "the curve arrived" — 54 videos were badged
"Curve Synced" with nothing behind it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.services.analysis_engine import build_actual_retention_updates

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
_STATS = {
    "avg_percentage_viewed": 61.2,
    "engagement_rate": 0.04,
    "views": 12345,
    "like_rate": 0.03,
    "comment_rate": 0.001,
    "views_per_subscriber": 1.4,
}


def test_a_real_curve_is_written():
    updates = build_actual_retention_updates(_STATS, {"performance_rating": 80}, {0.0: 1.0, 0.5: 0.6}, _NOW)
    assert updates["retention.actual_retention_curve"] == {"0.0": 1.0, "0.5": 0.6}


def test_an_empty_curve_is_not_written_at_all():
    """Absent, not empty — the UI distinguishes the two and only one is honest."""
    updates = build_actual_retention_updates(_STATS, None, {}, _NOW)
    assert "retention.actual_retention_curve" not in updates


def test_a_missing_curve_is_not_written_at_all():
    updates = build_actual_retention_updates(_STATS, None, None, _NOW)
    assert "retention.actual_retention_curve" not in updates


def test_the_other_actuals_are_backfilled_regardless_of_the_curve():
    """Losing the curve must not cost the metrics the outcome panel reads."""
    updates = build_actual_retention_updates(_STATS, {"performance_rating": 80}, None, _NOW)
    assert updates["retention.actual_views"] == 12345
    assert updates["retention.actual_avg_percentage_viewed"] == 61.2
    assert updates["retention.actual_engagement_rate"] == 0.04
    assert updates["retention.actual_performance_rating"] == 80
    assert updates["retention.actuals_populated_at"] == _NOW


def test_curve_keys_are_stringified_for_mongo():
    """Mongo keys must be strings; float keys would raise on write."""
    updates = build_actual_retention_updates(_STATS, None, {0.25: 0.8}, _NOW)
    assert list(updates["retention.actual_retention_curve"]) == ["0.25"]
