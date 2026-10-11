"""One day of Instagram account insights, parsed from the Graph API's bodies.

Pure: no I/O. Instagram reports follows and profile visits for the account as a
whole, never per reel, so these daily totals are what the per-reel follow
estimate is built from (see ``instagram_follow_attribution``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AccountDay:
    """Account totals for one IST day. ``None`` means Instagram did not report it.

    Instagram lags up to 48h, and the newest days come back with an empty
    breakdown rather than a 0 — so "not reported yet" must stay distinct from
    "nobody followed".
    """

    date: str
    profile_visits: int | None
    reel_views: int | None
    total_views: int | None
    follows: int | None
    unfollows: int | None
    interactions: int | None
    accounts_engaged: int | None


def _total_value(body: Mapping[str, Any] | None, name: str) -> Mapping[str, Any] | None:
    for entry in (body or {}).get("data", []):
        if entry.get("name") == name:
            total = entry.get("total_value")
            return total if isinstance(total, Mapping) else None
    return None


def _breakdown(total: Mapping[str, Any] | None) -> dict[str, int] | None:
    """``{dimension value: count}`` for a single-key breakdown; None when it has no results."""
    if total is None:
        return None
    for breakdown in total.get("breakdowns") or []:
        results = breakdown.get("results")
        if results is None:
            return None
        return {str(r["dimension_values"][0]): int(r.get("value") or 0) for r in results if r.get("dimension_values")}
    return None


def _value(total: Mapping[str, Any] | None) -> int | None:
    if total is None or "value" not in total:
        return None
    return int(total["value"] or 0)


def parse_account_day(
    date: str,
    totals_body: Mapping[str, Any] | None,
    views_body: Mapping[str, Any] | None,
    follows_body: Mapping[str, Any] | None,
) -> AccountDay:
    """Build an ``AccountDay`` from the three insight calls made for that day."""
    views_total = _total_value(views_body, "views")
    by_type = _breakdown(views_total)
    total_views = _value(views_total)
    reel_views: int | None = None
    if by_type is not None:
        reel_views = by_type.get("REEL", 0)

    follow_split = _breakdown(_total_value(follows_body, "follows_and_unfollows"))
    return AccountDay(
        date=date,
        profile_visits=_value(_total_value(totals_body, "profile_views")),
        reel_views=reel_views,
        total_views=total_views,
        # Meta's follow_type here: FOLLOWER = accounts that followed, NON_FOLLOWER =
        # accounts that unfollowed or left Instagram.
        follows=follow_split.get("FOLLOWER", 0) if follow_split is not None else None,
        unfollows=follow_split.get("NON_FOLLOWER", 0) if follow_split is not None else None,
        interactions=_value(_total_value(totals_body, "total_interactions")),
        accounts_engaged=_value(_total_value(totals_body, "accounts_engaged")),
    )
