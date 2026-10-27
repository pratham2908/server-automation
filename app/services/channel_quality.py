"""A channel's skip rate and watch time, as view-weighted sums kept with each sync's totals.

Pure: no I/O. Instagram reports these per reel only, so the channel's figure is built
from every reel in the sweep: a reel with more views counts for more. The sums are stored,
not the ratios, because a ratio of ratios can't be re-weighted later and a reel Instagram
returned no figure for must drop out of the numerator and the denominator alike.

The figures are lifetime ones over the whole catalogue, so they move slowly: one new
reel is a small share of everything watched so far.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# Channel metric -> (numerator field, denominator field, multiplier) on a stored totals document.
_LEVELS: dict[str, tuple[str, str, float]] = {
    "skip_rate": ("skipped_views", "skip_base", 100.0),
    "avg_watch_seconds": ("watch_seconds", "watch_base", 1.0),
    "watch_through": ("watch_share", "share_base", 1.0),
}


def channel_quality_sums(
    reels: Sequence[Mapping[str, Any]],
    insights: Mapping[str, Mapping[str, float]],
    durations: Mapping[str, float],
) -> dict[str, float]:
    """The weighted sums for one sweep; each denominator counts only the views behind its numerator."""
    sums = dict.fromkeys(
        ("skipped_views", "skip_base", "watch_seconds", "watch_base", "watch_share", "share_base"), 0.0
    )
    for reel in reels:
        row = insights.get(str(reel.get("id")))
        if not row or not row.get("views"):
            continue
        views = float(row["views"])
        if "reels_skip_rate" in row:
            sums["skipped_views"] += views * float(row["reels_skip_rate"]) / 100
            sums["skip_base"] += views
        if "ig_reels_video_view_total_time" in row:
            watched = float(row["ig_reels_video_view_total_time"]) / 1000
            sums["watch_seconds"] += watched
            sums["watch_base"] += views
            length = durations.get(str(reel.get("id")))
            if length and length > 0:
                sums["watch_share"] += watched / length
                sums["share_base"] += views
    return sums


def channel_level(totals_doc: Mapping[str, Any], metric: str) -> float | None:
    """The channel's skip rate (%), average watch time (s) or watch-through (0–1) at one sync; None if unrecorded."""
    fields = _LEVELS.get(metric)
    if fields is None:
        return None
    numerator, denominator, scale = fields
    top, base = totals_doc.get(numerator), totals_doc.get(denominator)
    if not isinstance(top, int | float) or not isinstance(base, int | float) or base <= 0:
        return None
    return float(top) / float(base) * scale
