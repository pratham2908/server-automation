"""Reading Threads insights replies into plain numbers.

Pure: no I/O.

The two insights endpoints answer in different shapes. Per post, every metric is
a lifetime number under ``values[0].value``. Per account, ``views`` is a series
(one value per day, ``end_time`` closing the day) while the other counts come as
one ``total_value`` for the whole window, and ``follower_demographics`` as
``total_value.breakdowns[0].results`` keyed by ``dimension_values``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

POST_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")
ACCOUNT_TOTAL_METRICS = ("likes", "replies", "reposts", "quotes")


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def post_insights(body: Mapping[str, Any] | None) -> dict[str, float]:
    """``{metric: value}`` from ``GET /{media-id}/insights``; a metric Threads left out is absent, not 0."""
    out: dict[str, float] = {}
    for entry in (body or {}).get("data") or []:
        name = entry.get("name")
        values = entry.get("values") or []
        value = _number(values[0].get("value")) if values else _number((entry.get("total_value") or {}).get("value"))
        if name and value is not None:
            out[str(name)] = value
    return out


def _day_of(end_time: str) -> str | None:
    """The day a series value covers. ``end_time`` marks the end of that day, so it is the day before."""
    try:
        end = datetime.fromisoformat(end_time.replace("Z", "+00:00").replace("+0000", "+00:00"))
    except ValueError:
        return None
    return (end.date() - timedelta(days=1)).isoformat()


def account_daily_views(body: Mapping[str, Any] | None) -> dict[str, float]:
    """``{date: views}`` from ``GET /{user-id}/threads_insights?metric=views``."""
    out: dict[str, float] = {}
    for entry in (body or {}).get("data") or []:
        if entry.get("name") != "views":
            continue
        for v in entry.get("values") or []:
            day = _day_of(str(v.get("end_time") or ""))
            value = _number(v.get("value"))
            if day and value is not None:
                out[day] = value
    return out


def account_totals(body: Mapping[str, Any] | None) -> dict[str, float]:
    """``{metric: total}`` for the single-number account metrics (likes, replies, followers_count…)."""
    out: dict[str, float] = {}
    for entry in (body or {}).get("data") or []:
        value = _number((entry.get("total_value") or {}).get("value"))
        if entry.get("name") and value is not None:
            out[str(entry["name"])] = value
    return out


def demographics(body: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """``[{value, count}]`` for one breakdown of ``follower_demographics``, largest first."""
    rows: list[dict[str, Any]] = []
    for entry in (body or {}).get("data") or []:
        for breakdown in (entry.get("total_value") or {}).get("breakdowns") or []:
            for result in breakdown.get("results") or []:
                dims = result.get("dimension_values") or []
                count = _number(result.get("value"))
                if dims and count is not None:
                    rows.append({"value": str(dims[0]), "count": int(count)})
    return sorted(rows, key=lambda r: r["count"], reverse=True)


def window_since(today: date, days: int) -> date:
    """The first day of a *days*-long window ending today."""
    return today - timedelta(days=days - 1)
