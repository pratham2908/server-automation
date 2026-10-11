"""Estimating how many follows and profile visits each reel brought in.

Pure: no I/O. Instagram's API refuses ``follows`` and ``profile_visits`` for
reels (they exist only for feed posts and stories), but it does report them for
the account per day. So each day's follows are shared out across the reels in
proportion to the views each reel got that day.

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


def estimate_follows(
    days: Sequence[AccountDay],
    readings_by_reel: Mapping[str, Sequence[ViewReading]],
) -> dict[str, FollowEstimate]:
    """Each reel's share of the account's follows and profile visits.

    A reel appears in the result only if at least one day with reported follows
    overlaps its views; a reel posted since then has nothing to estimate yet.
    """
    per_reel_days = {reel: views_by_day(readings) for reel, readings in readings_by_reel.items()}
    follows: dict[str, float] = {}
    visits: dict[str, float] = {}
    rough_follows: dict[str, float] = {}
    through: dict[str, str] = {}

    for day in sorted(days, key=lambda d: d.date):
        if day.follows is None:
            continue
        shares = {reel: by_day[day.date] for reel, by_day in per_reel_days.items() if day.date in by_day}
        tracked = sum(share.views for share in shares.values())
        if tracked <= 0:
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
        for reel, share in shares.items():
            weight = share.views / denominator
            follows[reel] = follows.get(reel, 0.0) + day_follows * weight
            visits[reel] = visits.get(reel, 0.0) + day_visits * weight
            if share.span_hours > ROUGH_SPAN_HOURS:
                rough_follows[reel] = rough_follows.get(reel, 0.0) + day_follows * weight
            through[reel] = day.date

    estimates: dict[str, FollowEstimate] = {}
    for reel, total in follows.items():
        rough = rough_follows.get(reel, 0.0)
        quality: Quality = "rough" if total > 0 and rough > ROUGH_SHARE_LIMIT * total else "good"
        estimates[reel] = FollowEstimate(
            follows=total, profile_visits=visits.get(reel, 0.0), quality=quality, through=through[reel]
        )
    return estimates
