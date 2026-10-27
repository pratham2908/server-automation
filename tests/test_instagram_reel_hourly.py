"""Hourly gains from a reel's cumulative readings."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.instagram_reel_hourly import MAX_HOURS, QUARTER, MetricReading, bucket_gains, floor_to, hourly_gains
from app.timezone import IST

T0 = datetime(2026, 10, 10, 9, 0, tzinfo=IST)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def reading(hours: float, **values: float) -> MetricReading:
    return MetricReading(at=at(hours), values=values)


def views(result: list) -> list[float | None]:
    return [h.values["views"] for h in result]


class TestHourlyGains:
    def test_no_readings_is_empty(self) -> None:
        assert hourly_gains(at(0), []) == []

    def test_readings_on_the_hour_give_each_hour_its_gain(self) -> None:
        result = hourly_gains(at(0), [reading(1, views=100), reading(2, views=160), reading(3, views=180)])
        assert [h.start for h in result] == [at(0), at(1), at(2)]
        assert views(result) == [100, 60, 20]

    def test_gain_is_split_across_hours_by_overlap(self) -> None:
        # Publish at 9:00; readings at 9:30 and 10:30.
        result = hourly_gains(at(0), [reading(0.5, views=50), reading(1.5, views=150)])
        # Only the 9:00 hour is complete; the 10:00 hour is still running at 10:30.
        assert [h.start for h in result] == [at(0)]
        assert views(result) == [pytest.approx(50 + 50)]

    def test_publish_mid_hour_counts_that_hour(self) -> None:
        result = hourly_gains(at(0.25), [reading(1, views=40), reading(2, views=70)])
        assert [h.start for h in result] == [at(0), at(1)]
        assert views(result) == [40, 30]

    def test_long_gap_leaves_hours_unknown(self) -> None:
        result = hourly_gains(at(0), [reading(1, views=10), reading(6, views=60), reading(7, views=70)])
        assert views(result) == [10, None, None, None, None, None, 10]

    def test_unknown_start_when_first_reading_is_mid_hour_without_publish(self) -> None:
        result = hourly_gains(None, [reading(0.5, views=10), reading(1.5, views=30), reading(2.5, views=50)])
        # 9:00 began before the first reading; 10:00 is fully covered.
        assert [h.start for h in result] == [at(0), at(1)]
        assert views(result) == [None, pytest.approx(20)]

    def test_drop_in_count_is_no_negative_hour(self) -> None:
        result = hourly_gains(at(0), [reading(1, views=100), reading(2, views=95), reading(3, views=120)])
        assert views(result) == [100, 0, 25]

    def test_metric_recorded_later_is_unknown_before(self) -> None:
        readings = [reading(1, views=10), reading(2, views=20, likes=3), reading(3, views=30, likes=5)]
        result = hourly_gains(at(-100), readings)
        # Publish long before: the gap to the first reading is unknown for both.
        assert [h.values["likes"] for h in result][-2:] == [None, 2]
        assert views(result)[-2:] == [10, 10]
        assert result[-1].values["shares"] is None

    def test_window_is_capped(self) -> None:
        readings = [reading(h, views=h * 10) for h in range(0, MAX_HOURS + 50)]
        result = hourly_gains(at(0), readings)
        assert len(result) == MAX_HOURS
        assert result[-1].start == at(MAX_HOURS + 48)

    def test_hours_are_ist(self) -> None:
        result = hourly_gains(at(0), [reading(1, views=1)])
        assert result[0].start.utcoffset() == timedelta(hours=5, minutes=30)


class TestQuarterGains:
    def test_floor_to_quarter_is_ist(self) -> None:
        assert floor_to(at(0.4), QUARTER) == at(0.25)
        assert floor_to(at(0.25), QUARTER) == at(0.25)

    def test_quarter_readings_place_each_quarter(self) -> None:
        readings = [reading(q / 4, views=q * 10) for q in range(1, 6)]
        result = bucket_gains(at(0), readings, QUARTER)
        assert [r.start for r in result] == [at(q / 4) for q in range(5)]
        assert views(result) == [10, 10, 10, 10, 10]

    def test_a_missed_sync_still_places(self) -> None:
        result = bucket_gains(at(0), [reading(0.25, views=10), reading(0.75, views=30), reading(1, views=40)], QUARTER)
        assert views(result) == [10, 10, 10, 10]

    def test_hourly_era_readings_are_gaps_not_a_smear(self) -> None:
        readings = [reading(1, views=100), reading(2, views=160), reading(2.25, views=170)]
        result = bucket_gains(None, readings, QUARTER)
        assert views(result) == [None, None, None, None, 10]


class TestHourlyLevels:
    def test_last_reading_in_each_hour_wins(self) -> None:
        from app.services.instagram_reel_hourly import hourly_levels

        readings = [
            reading(0.1, skip_rate=40.0, avg_view_duration_seconds=3.0),
            reading(0.8, skip_rate=42.0, avg_view_duration_seconds=3.4),
            reading(1.5, skip_rate=44.0),
        ]
        levels = hourly_levels(readings)
        assert levels[at(0)] == {"skip_rate": 42.0, "avg_view_duration_seconds": 3.4}
        assert levels[at(1)] == {"skip_rate": 44.0}

    def test_hours_without_readings_are_absent(self) -> None:
        from app.services.instagram_reel_hourly import hourly_levels

        assert hourly_levels([reading(0.5, views=10.0), reading(3.5, skip_rate=1.0)]).keys() == {at(3)}
