"""A reel's gains hour by hour (or quarter-hour by quarter-hour, IST), from the cumulative readings each sync records.

Pure: no I/O. Reels under a week old are read every 15 minutes, so the gain between
two readings is spread over the clock buckets they span by time overlap — a sync at
:35 past puts most of its hour's views in the hour before, some in the current.

A bucket is only reported when every moment of it sits inside a short gap between
readings. Across a long gap (before hourly syncing began, or once a reel is past
its first week and read daily) the views did happen, but nothing says in which
hour, so that hour is ``None`` rather than a flat smear that reads as a real
quiet spell.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.timezone import IST

# Cumulative counts each snapshot records, in the order the chart offers them.
HOURLY_METRICS = ("views", "likes", "comments", "shares", "saves")
# Rates and averages each snapshot records. Unlike the counts they are levels, not gains: the
# chart shows where each stood at the end of the hour, never a sum of them.
LEVEL_METRICS = ("skip_rate", "avg_view_duration_seconds")
# Readings further apart than this don't place their gain in a particular hour.
MAX_GAP_HOURS = 3.0
# Nor in a particular quarter-hour. Readings are 15 minutes apart, so this lets one missed
# sync through; the hourly readings from before 15-minute syncing show as gaps, not a smear.
QUARTER_MAX_GAP_HOURS = 0.75
# Longest stretch returned; a reel's frequent readings stop after its first week anyway.
MAX_HOURS = 14 * 24

HOUR = timedelta(hours=1)
QUARTER = timedelta(minutes=15)


@dataclass(frozen=True)
class MetricReading:
    """A reel's cumulative counts at one moment (timezone-aware); a metric is absent when not recorded."""

    at: datetime
    values: Mapping[str, float]


@dataclass(frozen=True)
class HourGains:
    # Start of the IST clock hour (or quarter-hour).
    start: datetime
    # Gain per metric in that bucket; None when the readings can't place it.
    values: dict[str, float | None]


def floor_to(at: datetime, step: timedelta) -> datetime:
    """Start of the IST bucket of length *step* (an hour or a divisor of one) that *at* falls in."""
    local = at.astimezone(IST)
    hour = local.replace(minute=0, second=0, microsecond=0)
    return hour + step * ((local - hour) // step)


def _metric_buckets(
    points: Sequence[tuple[datetime, float]], known_from: datetime, step: timedelta, max_gap_hours: float
) -> dict[datetime, float | None]:
    """Gain per bucket for one metric; buckets touched by a long gap map to None."""
    gains: dict[datetime, float | None] = {}
    for (start, before), (end, after) in zip(points, points[1:], strict=False):
        seconds = (end - start).total_seconds()
        if seconds <= 0:
            continue
        # Instagram occasionally revises a count down; that is no negative hour.
        gain = max(0.0, after - before)
        too_long = seconds / 3600 > max_gap_hours
        cursor = start
        while cursor < end:
            bucket = floor_to(cursor, step)
            chunk_end = min(bucket + step, end)
            if too_long or bucket < known_from:
                gains[bucket] = None
            elif bucket not in gains or gains[bucket] is not None:
                gains[bucket] = (gains.get(bucket) or 0.0) + gain * (chunk_end - cursor).total_seconds() / seconds
            cursor = chunk_end
    return gains


def hourly_gains(published_at: datetime | None, readings: Sequence[MetricReading]) -> list[HourGains]:
    """Each complete IST hour's gain per metric, oldest first, at most the last ``MAX_HOURS``."""
    return bucket_gains(published_at, readings, HOUR)


def bucket_gains(published_at: datetime | None, readings: Sequence[MetricReading], step: timedelta) -> list[HourGains]:
    """Each complete IST bucket's gain per metric, oldest first, covering at most the last ``MAX_HOURS``.

    *step* is ``HOUR`` or ``QUARTER``. The publish moment counts as a reading of
    zero for every metric, which is what lets a reel read often from the start
    show its first buckets.
    """
    max_gap_hours = MAX_GAP_HOURS if step >= HOUR else QUARTER_MAX_GAP_HOURS
    ordered = sorted(readings, key=lambda r: r.at)
    if not ordered:
        return []
    last_at = ordered[-1].at
    # Complete buckets only: the one the last reading falls in is still running.
    end_hour = floor_to(last_at, step)
    start_hour = end_hour - MAX_HOURS * HOUR

    series: dict[str, dict[datetime, float | None]] = {}
    for metric in HOURLY_METRICS:
        points = [(r.at, float(r.values[metric])) for r in ordered if metric in r.values]
        if not points:
            continue
        if published_at is not None and published_at < points[0][0]:
            points.insert(0, (published_at, 0.0))
            # Before publishing there was nothing to gain, so its bucket counts whole.
            known_from = floor_to(published_at, step)
        else:
            # The bucket the first reading lands in had views before it that nobody measured.
            first = points[0][0]
            known_from = floor_to(first, step) if floor_to(first, step) == first else floor_to(first, step) + step
        series[metric] = _metric_buckets(points, known_from, step, max_gap_hours)

    # Every hour any metric places, so a series asked for likes alone still has its hours.
    placed = set().union(*series.values()) if series else set()
    hours = sorted(h for h in placed if start_hour <= h < end_hour)
    if not hours:
        return []
    out: list[HourGains] = []
    hour = hours[0]
    while hour < end_hour:
        out.append(
            HourGains(start=hour, values={m: series[m].get(hour) if m in series else None for m in HOURLY_METRICS})
        )
        hour += step
    return out


def hourly_levels(
    readings: Sequence[MetricReading], metrics: Sequence[str] = LEVEL_METRICS
) -> dict[datetime, dict[str, float]]:
    """Each IST hour's last reading of every level metric, for the hours that have one.

    A level has no gap rule: a reading is the value at that moment whatever came
    before it, so an hour with no reading is simply absent.
    """
    levels: dict[datetime, dict[str, float]] = {}
    for reading in sorted(readings, key=lambda r: r.at):
        found = {m: float(reading.values[m]) for m in metrics if m in reading.values}
        if found:
            levels.setdefault(floor_to(reading.at, HOUR), {}).update(found)
    return levels
