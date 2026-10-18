"""Turning YouTube Analytics rows into stored fields."""

from __future__ import annotations

from app.services.youtube_analytics_parse import (
    age_gender_rows,
    daily_row,
    lifetime_metadata,
    retention_points,
    row_dicts,
    share_rows,
    swiped_away_rate,
)


class TestSwipedAwayRate:
    def test_share_of_views_that_did_not_stay(self) -> None:
        assert swiped_away_rate(200, 50) == 75.0

    def test_too_few_views_to_rate(self) -> None:
        assert swiped_away_rate(17, 4) is None

    def test_missing_counts(self) -> None:
        assert swiped_away_rate(None, 10) is None
        assert swiped_away_rate(100, None) is None

    def test_clamped_when_engaged_exceeds_views(self) -> None:
        assert swiped_away_rate(100, 120) == 0.0


class TestLifetimeMetadata:
    def test_maps_and_derives_fields(self) -> None:
        meta = lifetime_metadata(
            {
                "video": "v1",
                "views": 400,
                "engagedViews": 100,
                "estimatedMinutesWatched": 90,
                "averageViewDuration": 13.6,
                "averageViewPercentage": 108.75,
                "subscribersGained": 3,
                "subscribersLost": 1,
                "shares": 2,
            }
        )
        assert meta == {
            "analytics_views": 400,
            "engaged_views": 100,
            "swiped_away_rate": 75.0,
            "estimated_minutes_watched": 90.0,
            "estimated_hours_watched": 1.5,
            "avg_view_duration_seconds": 13.6,
            "avg_percentage_viewed": 108.75,
            "subscribers_gained": 3,
            "subscribers_lost": 1,
            "shares": 2,
        }

    def test_absent_metrics_are_left_out_not_zeroed(self) -> None:
        assert lifetime_metadata({"video": "v1", "views": 5}) == {"analytics_views": 5}


def test_daily_row() -> None:
    row = daily_row("ch", "v1", {"day": "2026-10-15", "views": 12, "engagedViews": 4, "subscribersGained": 1})
    assert row["date"] == "2026-10-15"
    assert row["views"] == 12.0
    assert row["subscribers_gained"] == 1.0
    assert row["swiped_away_rate"] is None  # under the view floor
    assert row["likes"] is None


def test_row_dicts_pairs_headers() -> None:
    assert row_dicts(["a", "b"], [[1, 2], [3, 4]]) == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]


def test_share_rows_sorted_and_zero_dropped() -> None:
    rows = [{"country": "ET", "views": 44}, {"country": "IN", "views": 623}, {"country": "X", "views": 0}]
    assert share_rows(rows, "country", "views") == [{"value": "IN", "count": 623.0}, {"value": "ET", "count": 44.0}]


def test_age_gender_strips_prefix() -> None:
    rows = [
        {"ageGroup": "age18-24", "gender": "male", "viewerPercentage": 100},
        {"ageGroup": "age25-34", "gender": "female", "viewerPercentage": 0},
    ]
    assert age_gender_rows(rows) == [{"age": "18-24", "gender": "male", "percent": 100.0}]


def test_retention_points_ordered_with_optional_relative() -> None:
    rows = [
        {"elapsedVideoTimeRatio": 0.5, "audienceWatchRatio": 0.4},
        {"elapsedVideoTimeRatio": 0.01, "audienceWatchRatio": 1.2, "relativeRetentionPerformance": 0.6},
    ]
    assert retention_points(rows) == [
        {"at": 0.01, "watching": 1.2, "relative": 0.6},
        {"at": 0.5, "watching": 0.4},
    ]
