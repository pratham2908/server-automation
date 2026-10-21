"""A reel's gains hour by hour (IST), from the cumulative readings each sync records.

Pure: no I/O. Reels under a week old are read every hour, so the gain between two
readings is spread over the clock hours they span by time overlap — a sync at
:35 past puts most of its hour's views in the hour before, some in the current.

An hour is only reported when every moment of it sits inside a short gap between
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
# Readings further apart than this don't place their gain in a particular hour.
MAX_GAP_HOURS = 3.0
# Longest stretch returned; a reel's hourly readings stop after its first week anyway.
MAX_HOURS = 14 * 24

_HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class MetricReading:
    """A reel's cumulative counts at one moment (timezone-aware); a metric is absent when not recorded."""

    at: datetime
    values: Mapping[str, float]


@dataclass(frozen=True)
class HourGains:
    # Start of the IST clock hour.
    start: datetime
    # Gain per metric in that hour; None when the readings can't place it.
    values: dict[str, float | None]


def _floor_hour(at: datetime) -> datetime:
    return at.astimezone(IST).replace(minute=0, second=0, microsecond=0)


def _metric_hours(points: Sequence[tuple[datetime, float]], known_from: datetime) -> dict[datetime, float | None]:
    """Gain per hour for one metric; hours touched by a long gap map to None."""
    gains: dict[datetime, float | None] = {}
    for (start, before), (end, after) in zip(points, points[1:], strict=False):
        seconds = (end - start).total_seconds()
        if seconds <= 0:
            continue
        # Instagram occasionally revises a count down; that is no negative hour.
        gain = max(0.0, after - before)
        too_long = seconds / 3600 > MAX_GAP_HOURS
        cursor = start
        while cursor < end:
            hour = _floor_hour(cursor)
            chunk_end = min(hour + _HOUR, end)
            if too_long or hour < known_from:
                gains[hour] = None
            elif hour not in gains or gains[hour] is not None:
                gains[hour] = (gains.get(hour) or 0.0) + gain * (chunk_end - cursor).total_seconds() / seconds
            cursor = chunk_end
    return gains


def hourly_gains(published_at: datetime | None, readings: Sequence[MetricReading]) -> list[HourGains]:
    """Each complete IST hour's gain per metric, oldest first, at most the last ``MAX_HOURS``.

    The publish moment counts as a reading of zero for every metric, which is
    what lets a reel read hourly from the start show its first hours.
    """
    ordered = sorted(readings, key=lambda r: r.at)
    if not ordered:
        return []
    last_at = ordered[-1].at
    # Complete hours only: the one the last reading falls in is still running.
    end_hour = _floor_hour(last_at)
    start_hour = end_hour - MAX_HOURS * _HOUR

    series: dict[str, dict[datetime, float | None]] = {}
    for metric in HOURLY_METRICS:
        points = [(r.at, float(r.values[metric])) for r in ordered if metric in r.values]
        if not points:
            continue
        if published_at is not None and published_at < points[0][0]:
            points.insert(0, (published_at, 0.0))
            # Before publishing there was nothing to gain, so its hour counts whole.
            known_from = _floor_hour(published_at)
        else:
            # The hour the first reading lands in had views before it that nobody measured.
            first = points[0][0]
            known_from = _floor_hour(first) if _floor_hour(first) == first else _floor_hour(first) + _HOUR
        series[metric] = _metric_hours(points, known_from)

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
        hour += _HOUR
    return out
