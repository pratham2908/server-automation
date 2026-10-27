"""Views over time for a channel or one video, the way YouTube Studio's realtime card shows them.

Pure: no I/O.

* **Hourly** ranges come from our own readings: each sync records cumulative
  counts, and the gain between two readings is spread over the clock hours (or
  quarter-hours) it spans (``instagram_reel_hourly.bucket_gains``). Neither
  platform reports views per hour, so this is the only way to see them.
* **Daily** ranges use the platform's own per-day numbers where it has them —
  Instagram's account totals, YouTube Analytics — since a day the platform
  counted beats one we reconstructed.

A channel's hourly total is the sum of every post's views at one sync. It is
only recorded when every post came back with a view count; a total missing a
post would read as a drop.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from app.services.instagram_content_analysis import views_at
from app.services.instagram_follow_attribution import ViewReading
from app.services.instagram_reel_hourly import (
    HOUR,
    MAX_GAP_HOURS,
    QUARTER,
    QUARTER_MAX_GAP_HOURS,
    MetricReading,
    bucket_gains,
    floor_to,
    hourly_gains,
)
from app.timezone import IST

Granularity = Literal["hour", "day"]
RangeKey = Literal["24h", "48h", "7d", "28d", "90d"]
Step = Literal["1h", "15m"]
# Clock: buckets on IST clock hours and dates. Posted: buckets counted from the video's publish time.
Align = Literal["clock", "posted"]
# Gain: what each bucket added. Total: the cumulative count at the bucket's end.
Measure = Literal["gain", "total"]

# Each range: how its bars are bucketed and how many there are.
RANGES: dict[str, tuple[Granularity, int]] = {
    "24h": ("hour", 24),
    "48h": ("hour", 48),
    "7d": ("hour", 7 * 24),
    "28d": ("day", 28),
    "90d": ("day", 90),
}

# Rates and averages rather than counts: each reading is where the figure stood, so a bucket has no
# "gain" — only the level at its end. The snapshot field each is read from; watch-through is the
# average watch time as a share of the reel's length, so it is scaled by the duration when loaded.
LEVEL_SNAPSHOT_FIELDS: dict[str, str] = {
    "skip_rate": "skip_rate",
    "avg_watch_seconds": "avg_view_duration_seconds",
    "watch_through": "avg_view_duration_seconds",
}

# The bucket an hourly range's bars are cut into.
STEPS: dict[str, timedelta] = {"1h": HOUR, "15m": QUARTER}
DAY = timedelta(days=1)
# A video past its first week is read about once a day, so day edges sit between readings
# up to a day apart; wider than this and the count at the edge would be a guess.
DAY_MAX_GAP_HOURS = 30.0


@dataclass(frozen=True)
class SeriesPoint:
    # ISO hour start (with the +05:30 offset) or ISO date.
    start: str
    # None when nothing places a value in this bucket — a gap, never a zero.
    value: float | None
    # The value was spread across a long gap between readings rather than measured in this bucket.
    rough: bool = False
    # Hours from the video's publish time to the bucket's start, on a since-posted series.
    offset_hours: float | None = None


def channel_totals(
    posts: Sequence[Mapping[str, Any]], insights: Mapping[str, Mapping[str, float]]
) -> dict[str, int] | None:
    """A profile's summed counts from one sweep, or None if any post lacks a view count.

    Likes and comments come on the listing; views, shares and saves from each post's insights.
    """
    totals = {"views": 0, "likes": 0, "comments": 0, "shares": 0, "saves": 0}
    for post in posts:
        row = insights.get(str(post.get("id")))
        if row is None or "views" not in row:
            return None
        totals["views"] += int(row["views"])
        totals["shares"] += int(row.get("shares", 0))
        totals["saves"] += int(row.get("saved", 0))
        totals["likes"] += int(post.get("like_count") or 0)
        totals["comments"] += int(post.get("comments_count") or 0)
    totals["posts"] = len(posts)
    return totals


def hourly_points(
    published_at: datetime | None,
    readings: Sequence[MetricReading],
    metric: str,
    now: datetime,
    hours: int,
    step: timedelta = HOUR,
) -> list[SeriesPoint]:
    """The complete IST buckets of *step* across the last *hours* before *now*, oldest first.

    The running bucket is left out.
    """
    gains = {h.start: h.values.get(metric) for h in bucket_gains(published_at, readings, step)}
    end = floor_to(now, step)
    count = int(hours * HOUR / step)
    starts = [end - step * (count - i) for i in range(count)]
    return [SeriesPoint(start=s.isoformat(), value=gains.get(s)) for s in starts]


def daily_from_readings(
    published_at: datetime | None, readings: Sequence[MetricReading], metric: str
) -> dict[str, float]:
    """Gains per IST date from our own readings — for a platform that reports no per-day number of its own.

    The hours a gap can't place are left out, so a day's value is a floor, never a guess.
    """
    out: dict[str, float] = {}
    for hour in hourly_gains(published_at, readings):
        value = hour.values.get(metric)
        if value is not None:
            day = hour.start.astimezone(IST).date().isoformat()
            out[day] = out.get(day, 0.0) + value
    return out


def daily_points(
    values_by_date: Mapping[str, float | None], rough_dates: set[str], today: date, days: int
) -> list[SeriesPoint]:
    """The last *days* dates up to *today*, oldest first; a date the source hasn't reported is a gap."""
    dates = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    return [SeriesPoint(start=d, value=values_by_date.get(d), rough=d in rough_dates) for d in dates]


def series_total(points: Sequence[SeriesPoint]) -> tuple[float, int]:
    """Sum of the buckets that have a value, and how many do."""
    known = [p.value for p in points if p.value is not None]
    return sum(known), len(known)


def reconstruct_channel_totals(
    readings_by_post: Mapping[str, Sequence[ViewReading]],
    published: Mapping[str, datetime | None],
    times: Sequence[datetime],
) -> list[tuple[datetime, float]]:
    """A channel's summed views at each past sync time, rebuilt from per-post readings (for backfill).

    Between two readings a post is interpolated, so an older post read once a day
    spreads its (small) share evenly over the hours; the newest posts, read
    hourly, carry the shape. Past its last reading a post is held at that value:
    the ones read rarely are the old ones that barely move. A time before some
    live post's first reading (and with no publish date to start it from zero)
    can't be summed and is skipped.
    """
    series: dict[str, list[ViewReading]] = {}
    for post_id, readings in readings_by_post.items():
        ordered = sorted(readings, key=lambda r: r.at)
        pub = published.get(post_id)
        if pub is not None and (not ordered or pub < ordered[0].at):
            ordered.insert(0, ViewReading(at=pub, views=0.0))
        if ordered:
            series[post_id] = ordered

    out: list[tuple[datetime, float]] = []
    for t in sorted(set(times)):
        total = 0.0
        complete = True
        for post_id, ordered in series.items():
            pub = published.get(post_id)
            if pub is not None and pub > t:
                continue  # not posted yet
            if ordered[0].at > t:
                complete = False
                break
            value = ordered[-1].views if ordered[-1].at <= t else views_at(ordered, t, float("inf"))
            if value is None:
                complete = False
                break
            total += value
        if complete:
            out.append((t, total))
    return out


def max_gap_hours_for(step: timedelta) -> float:
    """How far apart two readings may be for a count between them to be interpolated, per bucket size."""
    if step >= DAY:
        return DAY_MAX_GAP_HOURS
    return MAX_GAP_HOURS if step >= HOUR else QUARTER_MAX_GAP_HOURS


class Curve:
    """A cumulative count through time, from readings, read off at any moment by linear interpolation.

    Linear interpolation is the same assumption the per-bucket gains make when they spread a
    gain over the hours it spans, so a running total and its gains agree.
    """

    def __init__(self, published_at: datetime | None, readings: Sequence[MetricReading], metric: str) -> None:
        points = sorted((r.at, float(r.values[metric])) for r in readings if metric in r.values)
        if published_at is not None and (not points or published_at < points[0][0]):
            # Nothing before publishing: the publish moment is a free reading of zero.
            points.insert(0, (published_at, 0.0))
        self.times = [t for t, _ in points]
        self.values = [v for _, v in points]

    def at(self, moment: datetime, max_gap_hours: float) -> float | None:
        """The count at *moment*; None outside the readings or inside a gap wider than *max_gap_hours*."""
        i = bisect_left(self.times, moment)
        if i < len(self.times) and self.times[i] == moment:
            return self.values[i]
        if i == 0 or i == len(self.times):
            return None
        before, after = self.times[i - 1], self.times[i]
        span = (after - before).total_seconds()
        if span / 3600 > max_gap_hours:
            return None
        fraction = (moment - before).total_seconds() / span
        return self.values[i - 1] + (self.values[i] - self.values[i - 1]) * fraction


def curve_points(
    curve: Curve,
    starts: Sequence[datetime],
    step: timedelta,
    measure: Measure,
    now: datetime,
    origin: datetime | None = None,
) -> list[SeriesPoint]:
    """Each complete bucket's gain or end-of-bucket total, read off the curve at the bucket edges.

    Buckets still running at *now* are left out. With *origin* (a publish time) each point
    also carries its distance from it, for lining several videos up by age.
    """
    gap = max_gap_hours_for(step)
    out: list[SeriesPoint] = []
    for start in starts:
        end = start + step
        if end > now:
            break
        after = curve.at(end, gap)
        if measure == "total":
            value = after
        else:
            before = curve.at(start, gap)
            # A platform revising a count down is no negative bucket.
            value = None if after is None or before is None else max(0.0, after - before)
        offset = None if origin is None else round((start - origin).total_seconds() / 3600, 4)
        out.append(SeriesPoint(start=start.astimezone(IST).isoformat(), value=value, offset_hours=offset))
    return out


def window_starts(
    align: Align, step: timedelta, count: int, now: datetime, published_at: datetime | None
) -> list[datetime]:
    """The bucket starts for a window: the last *count* complete clock buckets, or the first *count* after posting."""
    if align == "posted":
        if published_at is None:
            return []
        return [published_at + step * i for i in range(count)]
    if step >= DAY:
        end = now.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        end = floor_to(now, step)
    return [end - step * (count - i) for i in range(count)]


def last_known(points: Sequence[SeriesPoint]) -> float | None:
    """The newest value — a running total's headline."""
    return next((p.value for p in reversed(points) if p.value is not None), None)
