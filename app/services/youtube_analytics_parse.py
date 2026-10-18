"""Turning YouTube Analytics report rows into the fields and documents we store.

Pure: no I/O. YouTube Analytics is a day behind at best (usually two to three),
reports whole days only, and withholds numbers for small audiences — so every
field here is present only when YouTube actually reported it.

``swiped_away_rate`` is YouTube's "viewed vs swiped away" for Shorts: a view is
counted when a Short starts playing, an engaged view only once the viewer stays
past the first moments. The share that didn't stay is the same thing Instagram
calls skip rate, which is why the content analysis can chart both the same way.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# Lifetime numbers per video, asked for in one batched report.
LIFETIME_METRICS = (
    "views",
    "engagedViews",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "subscribersGained",
    "subscribersLost",
    "shares",
)
# Per-day numbers for one video.
DAILY_METRICS = (
    "views",
    "engagedViews",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "subscribersGained",
    "subscribersLost",
    "likes",
    "shares",
    "comments",
)
CHANNEL_DAILY_METRICS = ("views", "engagedViews", "estimatedMinutesWatched", "subscribersGained", "subscribersLost")
# Below this many views one swipe moves the rate by whole percentage points.
MIN_VIEWS_FOR_SWIPE_RATE = 20


def row_dicts(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[dict[str, Any]]:
    return [dict(zip(headers, row, strict=False)) for row in rows]


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def swiped_away_rate(views: float | None, engaged_views: float | None) -> float | None:
    """Percent of views that left before becoming engaged views."""
    if views is None or engaged_views is None or views < MIN_VIEWS_FOR_SWIPE_RATE:
        return None
    return round(max(0.0, min(100.0, (1 - engaged_views / views) * 100)), 1)


def lifetime_metadata(row: Mapping[str, Any]) -> dict[str, float | int]:
    """``metadata`` fields for one video from its lifetime Analytics row.

    Analytics' own view count lags the live one from the Data API, so it is kept
    as ``analytics_views`` beside ``views`` rather than overwriting it — and the
    swipe rate is worked out from the two lagged counts, which agree with each other.
    """
    meta: dict[str, float | int] = {}
    views = _num(row.get("views"))
    engaged = _num(row.get("engagedViews"))
    if views is not None:
        meta["analytics_views"] = int(views)
    if engaged is not None:
        meta["engaged_views"] = int(engaged)
    rate = swiped_away_rate(views, engaged)
    if rate is not None:
        meta["swiped_away_rate"] = rate
    minutes = _num(row.get("estimatedMinutesWatched"))
    if minutes is not None:
        meta["estimated_minutes_watched"] = round(minutes, 1)
        meta["estimated_hours_watched"] = round(minutes / 60, 3)
    duration = _num(row.get("averageViewDuration"))
    if duration is not None:
        meta["avg_view_duration_seconds"] = round(duration, 1)
    percent = _num(row.get("averageViewPercentage"))
    if percent is not None:
        meta["avg_percentage_viewed"] = round(percent, 2)
    for source, target in (
        ("subscribersGained", "subscribers_gained"),
        ("subscribersLost", "subscribers_lost"),
        ("shares", "shares"),
    ):
        value = _num(row.get(source))
        if value is not None:
            meta[target] = int(value)
    return meta


def daily_row(channel_id: str, video_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """One ``youtube_video_daily`` document from a ``day``-dimension row."""
    views = _num(row.get("views"))
    engaged = _num(row.get("engagedViews"))
    return {
        "channel_id": channel_id,
        "youtube_video_id": video_id,
        "date": str(row["day"]),
        "views": views,
        "engaged_views": engaged,
        "swiped_away_rate": swiped_away_rate(views, engaged),
        "minutes_watched": _num(row.get("estimatedMinutesWatched")),
        "avg_view_duration_seconds": _num(row.get("averageViewDuration")),
        "avg_percentage_viewed": _num(row.get("averageViewPercentage")),
        "subscribers_gained": _num(row.get("subscribersGained")),
        "subscribers_lost": _num(row.get("subscribersLost")),
        "likes": _num(row.get("likes")),
        "shares": _num(row.get("shares")),
        "comments": _num(row.get("comments")),
    }


def channel_daily_row(channel_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "channel_id": channel_id,
        "date": str(row["day"]),
        "views": _num(row.get("views")),
        "engaged_views": _num(row.get("engagedViews")),
        "minutes_watched": _num(row.get("estimatedMinutesWatched")),
        "subscribers_gained": _num(row.get("subscribersGained")),
        "subscribers_lost": _num(row.get("subscribersLost")),
    }


def share_rows(rows: Sequence[Mapping[str, Any]], key: str, metric: str) -> list[dict[str, Any]]:
    """``[{value, count}]`` from a one-dimension report, largest first, zero rows dropped."""
    out = [
        {"value": str(r[key]), "count": float(r[metric])}
        for r in rows
        if r.get(key) is not None and isinstance(r.get(metric), int | float) and r[metric] > 0
    ]
    out.sort(key=lambda r: -r["count"])
    return out


def age_gender_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``[{age, gender, percent}]``; YouTube sends ``age18-24`` and ``female``/``male``/``user_specified``."""
    out = []
    for r in rows:
        percent = _num(r.get("viewerPercentage"))
        if percent is None or percent <= 0:
            continue
        out.append(
            {
                "age": str(r.get("ageGroup", "")).removeprefix("age"),
                "gender": str(r.get("gender", "")),
                "percent": percent,
            }
        )
    return out


def retention_points(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, float]]:
    """``[{at, watching, relative}]`` from an ``elapsedVideoTimeRatio`` report, in order.

    ``watching`` is the share of viewers still watching at that point of the video
    (above 1 where people rewatch); ``relative`` compares it with similar videos
    on YouTube (0.5 is typical), and may be missing.
    """
    points = []
    for r in rows:
        at = _num(r.get("elapsedVideoTimeRatio"))
        watching = _num(r.get("audienceWatchRatio"))
        if at is None or watching is None:
            continue
        point = {"at": at, "watching": watching}
        relative = _num(r.get("relativeRetentionPerformance"))
        if relative is not None:
            point["relative"] = relative
        points.append(point)
    points.sort(key=lambda p: p["at"])
    return points
