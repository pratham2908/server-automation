"""Studio-style views series: channel totals, hourly and daily buckets, and the backfill rebuild."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.services.instagram_follow_attribution import ViewReading
from app.services.instagram_reel_hourly import MetricReading
from app.services.view_series import (
    channel_totals,
    daily_points,
    hourly_points,
    reconstruct_channel_totals,
    series_total,
)
from app.timezone import IST

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=IST)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


class TestChannelTotals:
    def test_sums_every_post(self) -> None:
        posts = [{"id": "a", "like_count": 3, "comments_count": 1}, {"id": "b", "like_count": 2, "comments_count": 0}]
        insights = {"a": {"views": 100, "shares": 2, "saved": 1}, "b": {"views": 50}}
        assert channel_totals(posts, insights) == {
            "views": 150,
            "likes": 5,
            "comments": 1,
            "shares": 2,
            "saves": 1,
            "posts": 2,
        }

    def test_a_post_without_views_makes_the_total_unknown(self) -> None:
        # A short sum would read as the channel losing views.
        posts = [{"id": "a"}, {"id": "b"}]
        assert channel_totals(posts, {"a": {"views": 100}}) is None


class TestHourlyPoints:
    def test_full_range_ending_before_the_running_hour(self) -> None:
        readings = [MetricReading(at=at(h), values={"views": 100.0 * h}) for h in range(0, 5)]
        points = hourly_points(None, readings, "views", now=at(4.5), hours=6)
        assert [p.start for p in points] == [at(h).isoformat() for h in range(-2, 4)]
        assert [p.value for p in points] == [None, None, 100, 100, 100, 100]

    def test_other_metrics(self) -> None:
        readings = [MetricReading(at=at(h), values={"likes": 2.0 * h}) for h in range(0, 3)]
        points = hourly_points(None, readings, "likes", now=at(2), hours=2)
        assert [p.value for p in points] == [2, 2]


def test_daily_points_and_total() -> None:
    points = daily_points({"2026-10-18": 10.0, "2026-10-20": 30.0}, {"2026-10-20"}, date(2026, 10, 20), 4)
    assert [(p.start, p.value, p.rough) for p in points] == [
        ("2026-10-17", None, False),
        ("2026-10-18", 10.0, False),
        ("2026-10-19", None, False),
        ("2026-10-20", 30.0, True),
    ]
    assert series_total(points) == (40.0, 2)


class TestReconstruct:
    def test_interpolates_a_post_read_less_often(self) -> None:
        readings = {
            # Read hourly.
            "new": [ViewReading(at(h), 10.0 * h) for h in range(0, 4)],
            # Read only at 0h and 3h: its 30 views are spread evenly between.
            "old": [ViewReading(at(0), 1000.0), ViewReading(at(3), 1030.0)],
        }
        out = reconstruct_channel_totals(readings, {"new": None, "old": None}, [at(h) for h in range(0, 4)])
        assert out == [(at(0), 1000.0), (at(1), 1020.0), (at(2), 1040.0), (at(3), 1060.0)]

    def test_a_time_before_a_posts_first_reading_is_skipped(self) -> None:
        readings = {"a": [ViewReading(at(0), 0.0), ViewReading(at(1), 5.0)], "b": [ViewReading(at(1), 7.0)]}
        # At 0h, "b" (published long before) has no reading at or before — unknown, so skipped.
        out = reconstruct_channel_totals(readings, {"a": None, "b": None}, [at(0), at(1)])
        assert out == [(at(1), 12.0)]

    def test_a_post_is_held_at_its_last_reading(self) -> None:
        readings = {"new": [ViewReading(at(h), 10.0 * h) for h in range(0, 3)], "old": [ViewReading(at(0), 500.0)]}
        out = reconstruct_channel_totals(readings, {"new": None, "old": None}, [at(0), at(1), at(2)])
        assert out == [(at(0), 500.0), (at(1), 510.0), (at(2), 520.0)]

    def test_a_post_not_yet_published_counts_nothing(self) -> None:
        readings = {"a": [ViewReading(at(0), 50.0), ViewReading(at(2), 70.0)], "b": [ViewReading(at(2), 9.0)]}
        out = reconstruct_channel_totals(readings, {"a": None, "b": at(1.5)}, [at(1), at(2)])
        # At 1h "b" isn't posted; at 2h it has 9 (its publish moment is a free reading of 0).
        assert out == [(at(1), 60.0), (at(2), 79.0)]
