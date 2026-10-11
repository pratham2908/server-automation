"""Turning Instagram's per-reel insight numbers into the fields we store.

Pure: no I/O, so the units (milliseconds in, seconds out; percent as sent) are
tested rather than assumed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Everything the reel insights endpoint reports that the app shows. Asked for
# together first; if Instagram refuses the batch (a metric some reels lack), it
# is asked for one at a time so the others still arrive.
REEL_INSIGHT_METRICS: tuple[str, ...] = (
    "views",
    "reach",
    "saved",
    "shares",
    "total_interactions",
    "ig_reels_avg_watch_time",
    "ig_reels_video_view_total_time",
    "reels_skip_rate",
)


def reel_metadata(like_count: Any, comments_count: Any, insights: Mapping[str, float]) -> dict[str, float | int]:
    """The ``metadata`` fields for one reel.

    A field is present only when Instagram reported it. Writing a 0 for a metric
    it merely didn't return would overwrite a real earlier value on the next sync.
    """
    reach = int(insights.get("reach", 0))
    meta: dict[str, float | int] = {
        "likes": int(like_count or 0),
        "comments": int(comments_count or 0),
        "reach": reach,
        # ``views`` is the real play count. Reach (distinct accounts) used to stand
        # in for it, which undercounts any reel watched more than once per person.
        "views": int(insights["views"]) if "views" in insights else reach,
        "saves": int(insights.get("saved", 0)),
        "shares": int(insights.get("shares", 0)),
    }
    if "ig_reels_avg_watch_time" in insights:
        # Milliseconds from Instagram; seconds everywhere else in the app.
        meta["avg_view_duration_seconds"] = round(insights["ig_reels_avg_watch_time"] / 1000, 2)
    if "ig_reels_video_view_total_time" in insights:
        total_seconds = insights["ig_reels_video_view_total_time"] / 1000
        meta["total_watch_time_seconds"] = round(total_seconds, 1)
        meta["estimated_hours_watched"] = round(total_seconds / 3600, 3)
    if "reels_skip_rate" in insights:
        # Already a percentage (40 means 40% swiped away within the first moments).
        meta["skip_rate"] = round(float(insights["reels_skip_rate"]), 1)
    return meta
