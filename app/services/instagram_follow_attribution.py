"""Estimating how many follows, profile visits and bio-link taps each reel brought in.

Pure: no I/O. Instagram's API refuses ``follows``, ``profile_visits`` and
``profile_activity`` (bio-link taps) for reels — they exist only for feed posts
and stories — but it reports them for the account per day. So each day's totals
are shared out across the reels in proportion to the views each reel got that day.

A reel's views per day come from the cumulative view counts the sync records
(its publish time counts as 0 views). Views gained between two readings are
spread evenly over that stretch of time. When readings are far apart that
spreading is crude — reels are front-loaded — so such estimates are flagged
"rough".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.services.instagram_account_insights import AccountDay
from app.timezone import IST

# A stretch between two view readings longer than this can't place views on a
# particular day with any confidence.
ROUGH_SPAN_HOURS = 36.0
# An estimate is "rough" once more than this share of it rests on such stretches.
ROUGH_SHARE_LIMIT = 0.2

Quality = Literal["good", "rough"]


@dataclass(frozen=True)
class ViewReading:
    """A reel's cumulative view count at one moment (timezone-aware)."""

    at: datetime
    views: float


@dataclass(frozen=True)
class DayViews:
    views: float
    # Longest gap between readings that contributed to this day, in hours.
    span_hours: float


@dataclass(frozen=True)
class FollowEstimate:
    follows: float
    profile_visits: float
    bio_link_taps: float
    quality: Quality
    # Last day with reported follows that the estimate includes.
    through: str


def views_by_day(readings: Sequence[ViewReading]) -> dict[str, DayViews]:
    """Views a reel gained on each IST day, spreading each gap's gain by time overlap."""
    ordered = sorted(readings, key=lambda r: r.at)
    totals: dict[str, float] = {}
    spans: dict[str, float] = {}
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        seconds = (later.at - earlier.at).total_seconds()
        gain = later.views - earlier.views
        if seconds <= 0 or gain <= 0:
            continue
        span_hours = seconds / 3600
        cursor = earlier.at.astimezone(IST)
        end = later.at.astimezone(IST)
        while cursor < end:
            day_start = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
            next_day = day_start + timedelta(days=1)
            chunk_end = min(next_day, end)
            date = day_start.strftime("%Y-%m-%d")
            totals[date] = totals.get(date, 0.0) + gain * (chunk_end - cursor).total_seconds() / seconds
            spans[date] = max(spans.get(date, 0.0), span_hours)
            cursor = chunk_end
    return {date: DayViews(views=totals[date], span_hours=spans[date]) for date in totals}


@dataclass(frozen=True)
class ReelDay:
    """What one reel did on one IST day: its views, and its share of the account's actions."""

    date: str
    views: float
    # None when Instagram hasn't reported the account's follows for that day —
    # the last ~2 days (still filling in) or days older than our stored history.
    follows: float | None
    profile_visits: float | None
    bio_link_taps: float | None
    # The day's views were spread across a long gap between readings, so both the
    # views and the share of follows are guesses about which day they fell on.
    rough: bool


def daily_attribution(
    days: Sequence[AccountDay],
    readings_by_reel: Mapping[str, Sequence[ViewReading]],
) -> dict[str, list[ReelDay]]:
    """Each reel's views and estimated follows, profile visits and bio-link taps, day by day.

    Every day on which a reel gained views is listed, oldest first; days without
    reported follows carry ``None`` for the estimated actions rather than 0.
    """
    per_reel_days = {reel: views_by_day(readings) for reel, readings in readings_by_reel.items()}
    by_date = {day.date: day for day in days}
    timelines: dict[str, list[ReelDay]] = {}
    all_dates = sorted({date for by_day in per_reel_days.values() for date in by_day})

    for date in all_dates:
        shares = {reel: by_day[date] for reel, by_day in per_reel_days.items() if date in by_day}
        tracked = sum(share.views for share in shares.values())
        day = by_date.get(date)
        if day is None or day.follows is None or tracked <= 0:
            for reel, share in shares.items():
                timelines.setdefault(reel, []).append(
                    ReelDay(date, share.views, None, None, None, share.span_hours > ROUGH_SPAN_HOURS)
                )
            continue
        # With close readings, Instagram's own reel total for the day keeps the share
        # of reels we don't track (archived, older than our records) unattributed;
        # spreading can overshoot it, hence the max. With far-apart readings our
        # per-day views are smeared across days — a breakout reel's first day gets a
        # fraction of what it really did — so measuring them against Instagram's
        # total would hand most of the day to "untracked". Those days are shared
        # among the tracked reels alone.
        measured = all(share.span_hours <= ROUGH_SPAN_HOURS for share in shares.values())
        denominator = max(float(day.reel_views or 0), tracked) if measured else tracked
        # Follows from stories or feed posts aren't any reel's doing.
        reel_fraction = 1.0
        if day.total_views and day.reel_views is not None:
            reel_fraction = day.reel_views / day.total_views
        day_follows = day.follows * reel_fraction
        day_visits = (day.profile_visits or 0) * reel_fraction
        day_taps = (day.bio_link_taps or 0) * reel_fraction
        for reel, share in shares.items():
            weight = share.views / denominator
            timelines.setdefault(reel, []).append(
                ReelDay(
                    date,
                    share.views,
                    day_follows * weight,
                    day_visits * weight,
                    day_taps * weight,
                    share.span_hours > ROUGH_SPAN_HOURS,
                )
            )
    return timelines


def estimate_follows(
    days: Sequence[AccountDay],
    readings_by_reel: Mapping[str, Sequence[ViewReading]],
) -> dict[str, FollowEstimate]:
    """Each reel's share of the account's follows, profile visits and bio-link taps.

    A reel appears in the result only if at least one day with reported follows
    overlaps its views; a reel posted since then has nothing to estimate yet.
    """
    return estimates_from_timelines(daily_attribution(days, readings_by_reel))


def estimates_from_timelines(timelines: Mapping[str, Sequence[ReelDay]]) -> dict[str, FollowEstimate]:
    """Totals of :func:`daily_attribution`, so callers holding the timeline needn't recompute it."""
    estimates: dict[str, FollowEstimate] = {}
    for reel, timeline in timelines.items():
        reported = [entry for entry in timeline if entry.follows is not None]
        if not reported:
            continue
        total = sum(entry.follows or 0.0 for entry in reported)
        rough = sum(entry.follows or 0.0 for entry in reported if entry.rough)
        quality: Quality = "rough" if total > 0 and rough > ROUGH_SHARE_LIMIT * total else "good"
        estimates[reel] = FollowEstimate(
            follows=total,
            profile_visits=sum(entry.profile_visits or 0.0 for entry in reported),
            bio_link_taps=sum(entry.bio_link_taps or 0.0 for entry in reported),
            quality=quality,
            through=reported[-1].date,
        )
    return estimates
