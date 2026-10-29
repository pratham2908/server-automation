"""Spot Instagram insights that read lower than the reel already had.

Pure: no I/O. A reel's views only go up, but a sweep now and then returns ``views = 0`` for a reel
(seen 2026-10-29: two reels, ~756k views, for one sync). Stored, it dips the channel total and the
next sync's recovery reads as a one-hour spike of hundreds of thousands of views.
"""

from __future__ import annotations

from collections.abc import Mapping

# IG occasionally trims a reel's count by a few views; only a real fall is a glitch.
MIN_DROP = 100
MIN_DROP_SHARE = 0.05


def regressed_reels(insights: Mapping[str, Mapping[str, float]], previous_views: Mapping[str, float]) -> set[str]:
    """Media ids whose new views fall well below the last stored views."""
    bad: set[str] = set()
    for media_id, row in insights.items():
        before = previous_views.get(media_id)
        now = row.get("views")
        if before is None or now is None:
            continue
        drop = before - now
        if drop >= MIN_DROP and drop >= before * MIN_DROP_SHARE:
            bad.add(media_id)
    return bad
