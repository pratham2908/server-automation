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

# Only a Facebook-Login token gets these; the fetch adds them for that provider alone.
FACEBOOK_LOGIN_REEL_METRICS: tuple[str, ...] = ("reposts", "total_views")


def reel_metadata(like_count: Any, comments_count: Any, insights: Mapping[str, float]) -> dict[str, float | int]:
    """The ``metadata`` fields for one reel.

    A field is present only when Instagram reported it. Writing a 0 for a metric
    it merely didn't return would overwrite a real earlier value on the next sync.
    """
    # Likes and comments come from the media listing, which every synced reel
    # has; the rest come from the insights call, which can fail for one reel
    # (a transient error) and must then leave the stored numbers alone.
    meta: dict[str, float | int] = {
        "likes": int(like_count or 0),
        "comments": int(comments_count or 0),
    }
    reach = int(insights["reach"]) if "reach" in insights else 0
    if "reach" in insights:
        meta["reach"] = reach
    if "views" in insights:
        # The real play count. Reach (distinct accounts) used to stand in for it,
        # which undercounts any reel watched more than once per person.
        meta["views"] = int(insights["views"])
    elif "reach" in insights:
        meta["views"] = reach
    if "saved" in insights:
        meta["saves"] = int(insights["saved"])
    if "shares" in insights:
        meta["shares"] = int(insights["shares"])
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
    if "reposts" in insights:
        meta["reposts"] = int(insights["reposts"])
    if "total_views" in insights:
        # Includes plays of boosted/ad copies, which ``views`` leaves out.
        meta["total_views"] = int(insights["total_views"])
    if "total_interactions" in insights:
        interactions = int(insights["total_interactions"])
        meta["total_interactions"] = interactions
        if reach:
            # Interactions per reached account: comparable across reels of different size.
            meta["engagement_rate"] = round(interactions / reach * 100, 2)
    if reach and "views" in meta:
        # Plays per reached account; above 1 means people replayed it.
        meta["views_per_reach"] = round(meta["views"] / reach, 2)
    return meta
