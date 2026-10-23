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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from app.services.instagram_content_analysis import views_at
from app.services.instagram_follow_attribution import ViewReading
from app.services.instagram_reel_hourly import HOUR, QUARTER, MetricReading, bucket_gains, floor_to, hourly_gains
from app.timezone import IST

Granularity = Literal["hour", "day"]
RangeKey = Literal["48h", "7d", "28d", "90d"]
Step = Literal["1h", "15m"]

# Each range: how its bars are bucketed and how many there are.
RANGES: dict[str, tuple[Granularity, int]] = {
    "48h": ("hour", 48),
    "7d": ("hour", 7 * 24),
    "28d": ("day", 28),
    "90d": ("day", 90),
}

# The bucket an hourly range's bars are cut into.
STEPS: dict[str, timedelta] = {"1h": HOUR, "15m": QUARTER}


@dataclass(frozen=True)
class SeriesPoint:
    # ISO hour start (with the +05:30 offset) or ISO date.
    start: str
    # None when nothing places a value in this bucket — a gap, never a zero.
    value: float | None
    # The value was spread across a long gap between readings rather than measured in this bucket.
    rough: bool = False


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
