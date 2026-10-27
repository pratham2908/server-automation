"""Skip rate and watch time as levels on the views series."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.services.instagram_reel_hourly import MetricReading
from app.services.view_series import LEVEL_SNAPSHOT_FIELDS, STEPS, Curve, curve_points
from app.timezone import IST

T0 = datetime(2026, 10, 10, 9, 0, tzinfo=IST)


def test_each_level_metric_reads_a_snapshot_field() -> None:
    assert LEVEL_SNAPSHOT_FIELDS["watch_through"] == LEVEL_SNAPSHOT_FIELDS["avg_watch_seconds"]
    assert LEVEL_SNAPSHOT_FIELDS["skip_rate"] == "skip_rate"


def test_a_level_is_read_at_the_end_of_each_bucket_not_summed() -> None:
    readings = [
        MetricReading(at=T0 + timedelta(minutes=m), values={"skip_rate": v})
        for m, v in [(0, 50.0), (60, 40.0), (120, 30.0)]
    ]
    points = curve_points(
        Curve(None, readings, "skip_rate"), [T0, T0 + timedelta(hours=1)], STEPS["1h"], "total", T0 + timedelta(hours=3)
    )
    assert [p.value for p in points] == [40.0, 30.0]
