"""Instagram audience demographics and story insights, parsed from Graph API bodies.

Pure: no I/O. The sync shell (``instagram_audience_sync``) makes the calls and
stores what these functions return.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from app.timezone import assume_utc

# Our name for each audience → Meta's metric. Reached and engaged cover whoever
# saw / interacted in the timeframe; followers is the follower base itself.
AUDIENCE_METRICS: dict[str, str] = {
    "reached": "reached_audience_demographics",
    "engaged": "engaged_audience_demographics",
    "followers": "follower_demographics",
}
DEMOGRAPHIC_BREAKDOWNS: tuple[str, ...] = ("age", "gender", "country", "city")
TIMEFRAMES: tuple[str, ...] = ("this_week", "this_month")
# Demographics move slowly and cost one call per audience × breakdown × timeframe
# (24 per refresh), so one refresh a day is plenty.
AUDIENCE_MAX_AGE = timedelta(hours=20)

# Story metrics Instagram reports (probed live 2026-10-16). Navigation is asked
# separately because it needs its own breakdown.
STORY_METRICS: tuple[str, ...] = (
    "views",
    "reach",
    "replies",
    "shares",
    "total_interactions",
    "follows",
    "profile_visits",
    "profile_activity",
    "link_clicks",
)
STORY_LIFETIME = timedelta(hours=24)


def story_metrics_for(provider: str) -> tuple[str, ...]:
    """Instagram-login tokens are refused ``link_clicks``, which fails the whole request."""
    if provider == "instagram":
        return tuple(m for m in STORY_METRICS if m != "link_clicks")
    return STORY_METRICS


def parse_metric_values(body: Mapping[str, Any] | None) -> dict[str, int]:
    """``{metric: value}`` from a lifetime media-insights body."""
    out: dict[str, int] = {}
    for entry in (body or {}).get("data", []):
        name = entry.get("name")
        if not name:
            continue
        values = entry.get("values") or []
        raw = values[0].get("value", 0) if values else (entry.get("total_value") or {}).get("value", 0)
        out[str(name)] = int(raw or 0)
    return out


def parse_demographics(body: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """``[{value, count}]`` for one breakdown, largest first; empty when not reported."""
    out: list[dict[str, Any]] = []
    for entry in (body or {}).get("data", []):
        total = entry.get("total_value") or {}
        for breakdown in total.get("breakdowns") or []:
            for row in breakdown.get("results") or []:
                values = row.get("dimension_values") or []
                if values:
                    out.append({"value": str(values[0]), "count": int(row.get("value") or 0)})
    return sorted(out, key=lambda r: r["count"], reverse=True)


def audience_is_stale(last_fetched: datetime | None, now: datetime) -> bool:
    return last_fetched is None or now - assume_utc(last_fetched) >= AUDIENCE_MAX_AGE


def parse_story_navigation(body: Mapping[str, Any] | None) -> dict[str, int]:
    """``{tap_forward: n, tap_back: n, tap_exit: n, swipe_forward: n}`` from a navigation breakdown."""
    out: dict[str, int] = {}
    for entry in (body or {}).get("data", []):
        total = entry.get("total_value") or {}
        for breakdown in total.get("breakdowns") or []:
            for row in breakdown.get("results") or []:
                values = row.get("dimension_values") or []
                if values:
                    out[str(values[0]).lower()] = int(row.get("value") or 0)
    return out


def story_is_live(posted_at: datetime | None, now: datetime) -> bool:
    return posted_at is not None and now - assume_utc(posted_at) < STORY_LIFETIME
