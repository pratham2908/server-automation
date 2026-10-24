"""Trending: views gained over the last day, for fresh and old videos alike."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.services.instagram_follow_attribution import ViewReading
from app.services.views_trending import recent_gain
from app.timezone import IST

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=IST)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def test_gain_over_the_day_before_the_newest_reading() -> None:
    readings = [ViewReading(at(h), 100.0 * h) for h in range(0, 49, 6)]
    assert recent_gain(None, readings, 24) == 2400


def test_an_old_video_read_once_a_day_still_trends() -> None:
    # Daily readings: the count a day before the newest is interpolated between two of them.
    readings = [ViewReading(at(0), 1000.0), ViewReading(at(23), 1100.0), ViewReading(at(46), 3400.0)]
    assert recent_gain(None, readings, 24) == 3400 - (1000 + 100 * 22 / 23)


def test_posted_inside_the_window_counts_everything() -> None:
    assert recent_gain(at(10), [ViewReading(at(12), 500.0), ViewReading(at(20), 900.0)], 24) == 900


def test_no_reading_far_enough_back_is_unknown() -> None:
    assert recent_gain(None, [ViewReading(at(10), 50.0), ViewReading(at(20), 90.0)], 24) is None
    assert recent_gain(None, [], 24) is None


def test_a_count_revised_down_is_no_negative_trend() -> None:
    assert recent_gain(None, [ViewReading(at(0), 500.0), ViewReading(at(24), 480.0)], 24) == 0
