"""Studio-style views series: channel totals, hourly and daily buckets, and the backfill rebuild."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.services.instagram_follow_attribution import ViewReading
from app.services.instagram_reel_hourly import HOUR, QUARTER, MetricReading
from app.services.view_series import (
    DAY,
    Curve,
    channel_totals,
    curve_points,
    daily_points,
    hourly_points,
    last_known,
    parse_range,
    reconstruct_channel_totals,
    series_total,
    today_so_far,
    window_starts,
    with_running_today,
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

    def test_quarter_hour_bars(self) -> None:
        readings = [MetricReading(at=at(q / 4), values={"views": 5.0 * q}) for q in range(0, 9)]
        points = hourly_points(None, readings, "views", now=at(2.1), hours=1, step=QUARTER)
        assert [p.start for p in points] == [at(1 + q / 4).isoformat() for q in range(4)]
        assert [p.value for p in points] == [5, 5, 5, 5]


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


class TestCurve:
    """Since-posted and running-total series, read off the cumulative curve at bucket edges."""

    def _curve(self, published=None):
        readings = [MetricReading(at=at(h), values={"views": 100.0 * h}) for h in (1, 2, 3, 4)]
        return Curve(published, readings, "views")

    def test_interpolates_between_readings_and_stops_outside_them(self) -> None:
        curve = self._curve()
        assert curve.at(at(2), 3) == 200
        assert curve.at(at(2.5), 3) == 250
        assert curve.at(at(0.5), 3) is None and curve.at(at(5), 3) is None

    def test_a_long_gap_is_not_interpolated(self) -> None:
        curve = Curve(None, [MetricReading(at(0), {"views": 0.0}), MetricReading(at(10), {"views": 1000.0})], "views")
        assert curve.at(at(5), 3) is None
        assert curve.at(at(5), 30) == 500

    def test_publish_is_a_free_zero(self) -> None:
        assert self._curve(published=at(0)).at(at(0.5), 3) == 50

    def test_since_posted_gains_line_up_by_age(self) -> None:
        published = at(0.5)
        curve = Curve(
            published,
            [MetricReading(at=at(0.5 + h), values={"views": 10.0 * h * h}) for h in (1, 2, 3)],
            "views",
        )
        starts = window_starts("posted", HOUR, 4, now=at(10), published_at=published)
        points = curve_points(curve, starts, HOUR, "gain", now=at(10), origin=published)
        assert [p.offset_hours for p in points] == [0, 1, 2, 3]
        assert [p.value for p in points] == [10, 30, 50, None]

    def test_running_total_is_the_count_at_each_bucket_end(self) -> None:
        curve = self._curve(published=at(0))
        starts = window_starts("clock", HOUR, 3, now=at(3.5), published_at=None)
        points = curve_points(curve, starts, HOUR, "total", now=at(3.5))
        assert [p.value for p in points] == [100, 200, 300]
        assert last_known(points) == 300

    def test_buckets_still_running_are_left_out(self) -> None:
        starts = window_starts("posted", HOUR, 48, now=at(5.2), published_at=at(0))
        points = curve_points(self._curve(published=at(0)), starts, HOUR, "total", now=at(5.2), origin=at(0))
        assert len(points) == 5

    def test_daily_clock_window_ends_at_todays_midnight(self) -> None:
        starts = window_starts("clock", DAY, 2, now=datetime(2026, 10, 24, 15, 0, tzinfo=IST), published_at=None)
        assert starts == [datetime(2026, 10, 22, tzinfo=IST), datetime(2026, 10, 23, tzinfo=IST)]


class TestParseRange:
    def test_fixed_keys_keep_their_bars(self) -> None:
        assert parse_range("48h") == ("hour", 48)
        assert parse_range("28d") == ("day", 28)

    def test_custom_windows_open_hourly_up_to_a_week(self) -> None:
        assert parse_range("5d") == ("hour", 120)
        assert parse_range("36h") == ("hour", 36)
        assert parse_range("10d") == ("day", 10)

    def test_the_other_kind_of_bar_can_be_asked_for(self) -> None:
        assert parse_range("28d", "hour") == ("hour", 672)
        assert parse_range("5d", "day") == ("day", 5)
        # A part day still gets its own bar.
        assert parse_range("36h", "day") == ("day", 2)

    def test_too_long_or_not_a_window(self) -> None:
        assert parse_range("90d", "hour") is None
        assert parse_range("400d") is None
        assert parse_range("0d") is None
        assert parse_range("week") is None


class TestHourlyBeyondTwoWeeks:
    def test_a_28_day_hourly_range_reaches_back_past_the_default_cap(self) -> None:
        readings = [MetricReading(at=at(h), values={"views": 10.0 * h}) for h in range(0, 28 * 24 + 1)]
        points = hourly_points(None, readings, "views", now=at(28 * 24 + 0.5), hours=28 * 24)
        assert len(points) == 28 * 24
        assert all(p.value == 10 for p in points)


class TestRunningToday:
    MIDNIGHT = datetime(2026, 11, 6, 0, 0, tzinfo=IST)

    def reading(self, hours: float, views: float) -> MetricReading:
        return MetricReading(at=self.MIDNIGHT + timedelta(hours=hours), values={"views": views})

    def test_today_so_far_sums_todays_complete_hours(self) -> None:
        # 100 an hour from 10 PM yesterday; now is 3:20 AM, so today's complete hours are 0–3.
        readings = [self.reading(h, 1000.0 + 100 * (h + 2)) for h in range(-2, 4)]
        now = self.MIDNIGHT + timedelta(hours=3, minutes=20)
        total, through = today_so_far(None, readings, "views", now)
        assert total == 300
        assert through == (self.MIDNIGHT + timedelta(hours=3)).isoformat()

    def test_nothing_today_yet(self) -> None:
        readings = [self.reading(-3, 100.0), self.reading(-2, 200.0)]
        assert today_so_far(None, readings, "views", self.MIDNIGHT + timedelta(minutes=30)) is None

    def test_an_empty_today_is_filled_and_marked_running(self) -> None:
        points = daily_points({"2026-11-05": 50.0}, set(), date(2026, 11, 6), 2)
        out = with_running_today(points, date(2026, 11, 6), (123.44, "x"))
        assert [(p.value, p.running) for p in out] == [(50.0, False), (123.4, True)]

    def test_a_reported_today_keeps_its_number_but_is_still_running(self) -> None:
        points = daily_points({"2026-11-06": 80.0}, set(), date(2026, 11, 6), 1)
        out = with_running_today(points, date(2026, 11, 6), (10.0, "x"))
        assert [(p.value, p.running) for p in out] == [(80.0, True)]

    def test_a_window_not_ending_today_is_untouched(self) -> None:
        points = daily_points({}, set(), date(2026, 11, 5), 1)
        assert with_running_today(points, date(2026, 11, 6), (10.0, "x")) == points
