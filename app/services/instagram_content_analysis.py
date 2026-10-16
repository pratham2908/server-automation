"""How each reel performed beyond its lifetime totals, and what that says about content.

Pure: no I/O. Built from the cumulative view readings the sync records (plus the
publish moment as a free reading of 0).

There is deliberately no per-reel "follows per 1,000 views". The follow estimate
shares each day's account follows out by views, so every reel live that day gets
the same rate — a reel's rate would only say which days it got its views on, not
how well it converts. Shares and saves are counted per reel by Instagram, so
those rates do compare reels.

Every figure is ``None`` when the readings can't support it — a reel published
before we started recording has no measured first day, and a reel two days old
has no 7-day number yet. Guessing those would make old and new reels look
comparable when they aren't.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median
from typing import Literal

from app.services.instagram_follow_attribution import ReelDay, ViewReading
from app.timezone import IST, assume_utc

# Milestones measured after publishing, in hours.
SPEED_MARKS_HOURS = (24, 48, 168)
# A value between two readings is only interpolated when they are close relative
# to how far after publishing it lies; reels are front-loaded, so a straight line
# across a long early gap badly understates the first day.
GAP_SHARE_OF_MARK = 0.5
MIN_GAP_LIMIT_HOURS = 6.0
# Crossing times ("half its views by hour N") need readings at most this far apart.
CROSSING_GAP_LIMIT_HOURS = 24.0
# "Most of its views" is judged only once a reel has had a week to collect them.
MATURE_AFTER_HOURS = 168.0
# Picking up again: a reel at least a week old whose last two days ran at least
# PICKUP_RATIO times faster than the three days before, and gained enough views
# for that not to be noise.
PICKUP_RECENT_HOURS = 48.0
PICKUP_BASELINE_HOURS = 72.0
PICKUP_RATIO = 2.0
PICKUP_MIN_VIEWS = 50.0
# Below this many views a per-1k rate swings wildly on one share.
MIN_VIEWS_FOR_RATES = 100.0
# Instagram can still be filling in a day's follows for up to 48h, so the last
# this-many days (today included) are waiting rather than missing.
PENDING_DAYS = 3

DayStatus = Literal["estimated", "pending", "unavailable"]


@dataclass(frozen=True)
class ViewSpeed:
    views_24h: float | None
    views_48h: float | None
    views_7d: float | None
    hours_to_half: float | None
    hours_to_90: float | None
    picking_up: bool | None


@dataclass(frozen=True)
class ReelSummary:
    """One reel as the content analysis sees it, read back from its video doc."""

    media_id: str
    category: str | None
    published_at: datetime | None
    views: float | None
    skip_rate: float | None
    watch_through: float | None
    shares_per_1k: float | None
    saves_per_1k: float | None
    views_24h: float | None


@dataclass(frozen=True)
class PostingSlot:
    """Reels posted in one IST hour (0–23) or weekday (0 = Monday)."""

    slot: int
    reels: int
    # Reels whose first 24h we measured, and their median first-day views.
    measured: int
    median_views_24h: float | None
    # Reels at least a week old, and their median lifetime views.
    mature: int
    median_views: float | None


@dataclass(frozen=True)
class GroupStat:
    """Reels sharing one category or one content-param value."""

    dimension: str
    value: str
    reels: int
    median_views: float | None
    median_skip_rate: float | None
    median_watch_through: float | None
    median_shares_per_1k: float | None
    median_saves_per_1k: float | None


def parse_published_at(value: object) -> datetime | None:
    """Older video docs hold ``published_at`` as an ISO string, newer ones as a datetime."""
    if isinstance(value, datetime):
        return assume_utc(value)
    if isinstance(value, str) and value:
        try:
            return assume_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def day_status(day: str, has_follows: bool, today: date) -> DayStatus:
    """Whether a timeline day's follow estimate exists, is still to come, or never will."""
    if has_follows:
        return "estimated"
    if date.fromisoformat(day) > today - timedelta(days=PENDING_DAYS):
        return "pending"
    return "unavailable"


def _with_publish(published_at: datetime | None, readings: Iterable[ViewReading]) -> list[ViewReading]:
    ordered = sorted(readings, key=lambda r: r.at)
    if published_at is not None and (not ordered or published_at < ordered[0].at):
        ordered.insert(0, ViewReading(at=published_at, views=0.0))
    return ordered


def views_at(readings: Sequence[ViewReading], at: datetime, max_gap_hours: float) -> float | None:
    """Cumulative views at *at*, interpolated between the readings either side of it.

    ``None`` when *at* lies outside the readings or the two around it are more than
    *max_gap_hours* apart. *readings* must be sorted by time.
    """
    # A reading at that very moment needs no interpolation, whatever gap precedes it.
    for reading in readings:
        if reading.at == at:
            return reading.views
    for earlier, later in zip(readings, readings[1:], strict=False):
        if earlier.at < at < later.at:
            span = (later.at - earlier.at).total_seconds()
            if span / 3600 > max_gap_hours:
                return None
            fraction = (at - earlier.at).total_seconds() / span
            return earlier.views + (later.views - earlier.views) * fraction
    return None


def _hours_to_reach(readings: Sequence[ViewReading], published_at: datetime, target: float) -> float | None:
    for earlier, later in zip(readings, readings[1:], strict=False):
        if later.views < target:
            continue
        if earlier.views >= target:
            return max(0.0, (earlier.at - published_at).total_seconds() / 3600)
        span = (later.at - earlier.at).total_seconds()
        if span / 3600 > CROSSING_GAP_LIMIT_HOURS:
            return None
        fraction = (target - earlier.views) / (later.views - earlier.views)
        crossed = earlier.at + timedelta(seconds=span * fraction)
        return (crossed - published_at).total_seconds() / 3600
    return None


def _picking_up(readings: Sequence[ViewReading], published_at: datetime) -> bool | None:
    last = readings[-1]
    if (last.at - published_at).total_seconds() / 3600 < MATURE_AFTER_HOURS:
        return None
    split = last.at - timedelta(hours=PICKUP_RECENT_HOURS)
    start = split - timedelta(hours=PICKUP_BASELINE_HOURS)
    at_split = views_at(readings, split, CROSSING_GAP_LIMIT_HOURS)
    at_start = views_at(readings, start, CROSSING_GAP_LIMIT_HOURS)
    if at_split is None or at_start is None:
        return None
    recent = last.views - at_split
    if recent < PICKUP_MIN_VIEWS:
        return False
    baseline_rate = (at_split - at_start) / PICKUP_BASELINE_HOURS
    return recent / PICKUP_RECENT_HOURS >= PICKUP_RATIO * baseline_rate


def view_speed(published_at: datetime | None, readings: Sequence[ViewReading]) -> ViewSpeed:
    """How fast a reel collected its views, from its cumulative readings (timezone-aware)."""
    if published_at is None:
        return ViewSpeed(None, None, None, None, None, None)
    ordered = _with_publish(published_at, readings)
    if len(ordered) < 2:
        return ViewSpeed(None, None, None, None, None, None)
    marks = [
        views_at(ordered, published_at + timedelta(hours=h), max(MIN_GAP_LIMIT_HOURS, GAP_SHARE_OF_MARK * h))
        for h in SPEED_MARKS_HOURS
    ]
    last = ordered[-1]
    hours_to_half = hours_to_90 = None
    if (last.at - published_at).total_seconds() / 3600 >= MATURE_AFTER_HOURS and last.views > 0:
        hours_to_half = _hours_to_reach(ordered, published_at, 0.5 * last.views)
        hours_to_90 = _hours_to_reach(ordered, published_at, 0.9 * last.views)
    return ViewSpeed(marks[0], marks[1], marks[2], hours_to_half, hours_to_90, _picking_up(ordered, published_at))


def per_1k(count: float | None, views: float | None) -> float | None:
    """*count* per 1,000 views, once there are enough views for the rate to mean something."""
    if count is None or views is None or views < MIN_VIEWS_FOR_RATES:
        return None
    return count / views * 1000


def watch_through(avg_watch_seconds: float | None, duration_seconds: float | None) -> float | None:
    """Average watch time as a share of the reel's length (can pass 1.0 on replays)."""
    if avg_watch_seconds is None or not duration_seconds or duration_seconds <= 0:
        return None
    return avg_watch_seconds / duration_seconds


def _median(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return median(present) if present else None


def _is_mature(reel: ReelSummary, now: datetime) -> bool:
    return reel.published_at is not None and (now - reel.published_at).total_seconds() / 3600 >= MATURE_AFTER_HOURS


def posting_slots(reels: Sequence[ReelSummary], now: datetime) -> tuple[list[PostingSlot], list[PostingSlot]]:
    """Reels grouped by the IST hour and the IST weekday they were posted, emptiest slots left out."""
    by_hour: dict[int, list[ReelSummary]] = {}
    by_weekday: dict[int, list[ReelSummary]] = {}
    for reel in reels:
        if reel.published_at is None:
            continue
        local = reel.published_at.astimezone(IST)
        by_hour.setdefault(local.hour, []).append(reel)
        by_weekday.setdefault(local.weekday(), []).append(reel)

    def slot(key: int, group: list[ReelSummary]) -> PostingSlot:
        measured = [r.views_24h for r in group if r.views_24h is not None]
        mature = [r.views for r in group if r.views is not None and _is_mature(r, now)]
        return PostingSlot(
            slot=key,
            reels=len(group),
            measured=len(measured),
            median_views_24h=_median(measured),
            mature=len(mature),
            median_views=_median(mature),
        )

    return (
        [slot(k, by_hour[k]) for k in sorted(by_hour)],
        [slot(k, by_weekday[k]) for k in sorted(by_weekday)],
    )


def flatten_params(params: object) -> dict[str, list[str]]:
    """A video's ``content_params`` as ``{name: [values]}``, keeping only plain values.

    Lists contribute each item; nested objects are skipped because they have no
    single value to group by.
    """
    if not isinstance(params, Mapping):
        return {}
    flat: dict[str, list[str]] = {}
    for name, value in params.items():
        items = value if isinstance(value, list) else [value]
        values = [str(item) for item in items if isinstance(item, str | int | float | bool) and str(item).strip()]
        if values:
            flat[str(name)] = values
    return flat


def content_groups(
    reels: Sequence[ReelSummary], param_values: Mapping[str, Mapping[str, Sequence[str]]]
) -> list[GroupStat]:
    """Per-category and per-content-param averages; *param_values* is keyed by media id."""
    groups: dict[tuple[str, str], list[ReelSummary]] = {}
    for reel in reels:
        groups.setdefault(("category", reel.category or "Uncategorized"), []).append(reel)
        for name, values in param_values.get(reel.media_id, {}).items():
            for value in values:
                groups.setdefault((name, value), []).append(reel)

    stats = [
        GroupStat(
            dimension=dimension,
            value=value,
            reels=len(members),
            median_views=_median(r.views for r in members),
            median_skip_rate=_median(r.skip_rate for r in members),
            median_watch_through=_median(r.watch_through for r in members),
            median_shares_per_1k=_median(r.shares_per_1k for r in members),
            median_saves_per_1k=_median(r.saves_per_1k for r in members),
        )
        for (dimension, value), members in groups.items()
    ]
    stats.sort(key=lambda g: (g.dimension != "category", g.dimension, -g.reels, g.value))
    return stats
