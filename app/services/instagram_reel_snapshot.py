"""What each sync records about a reel, and how a trial reel's graduation is noticed.

Pure: no I/O. Instagram's per-reel insights are lifetime totals with no history of
their own, so the only way to see how a reel moved is to keep a reading every sync.
Each reading holds every number we track, plus whether the reel is on the profile grid.

A trial reel starts off the grid and joins it when it is graduated, so the first sync
that sees it on the grid is the nearest thing to a graduation time the API offers.
``is_shared_to_feed`` is not a trial flag (a normal reel can be off the grid too), so
it is only read as graduation for a reel we know is a trial.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

# Whole-number counts, as the hourly chart reads them.
COUNT_METRICS = ("views", "likes", "comments", "shares", "saves")
# Measures that are not counts: reach is whole accounts, the others carry decimals.
MEASURE_METRICS = ("reach", "skip_rate", "avg_view_duration_seconds", "total_watch_time_seconds")


def snapshot_values(metadata: Mapping[str, Any], shared_to_feed: bool | None) -> dict[str, float | int | bool]:
    """The fields of one reading. A number Instagram did not report this time is left out, never zeroed."""
    values: dict[str, float | int | bool] = {k: int(metadata[k]) for k in COUNT_METRICS if k in metadata}
    values.update({k: float(metadata[k]) for k in MEASURE_METRICS if k in metadata})
    if shared_to_feed is not None:
        values["shared_to_feed"] = shared_to_feed
    return values


def graduation_fields(
    video: Mapping[str, Any], shared_to_feed: bool | None, now: datetime, previous_at: datetime | None
) -> dict[str, Any]:
    """The video fields to write after a sync has seen whether the reel is on the grid.

    Always records the latest grid state. For a reel we know is a trial it also stamps
    ``graduated_at`` the first time it is seen on the grid after being seen off it, with
    ``graduated_after`` (the reading before) bounding how long ago that could have been.

    A trial first seen already on the grid cannot be dated: it may have been graduated any
    time before we looked, so it is marked unknown rather than given the moment we noticed.
    """
    if shared_to_feed is None:
        return {}
    fields: dict[str, Any] = {"shared_to_feed": shared_to_feed}
    if not video.get("trial_graduation") or video.get("graduated_at") or video.get("graduation_time_unknown"):
        return fields
    if not shared_to_feed:
        return fields
    if video.get("shared_to_feed") is False:
        fields.update({"graduated_at": now, "graduated_after": previous_at, "graduated_detected_by": "grid"})
    else:
        fields["graduation_time_unknown"] = True
    return fields
