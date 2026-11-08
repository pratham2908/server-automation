"""The numbers on the Followers page for a chosen period: per day, in total, and per reel.

Follows and unfollows per day are Instagram's own (``instagram_account_daily``), counted on its
Pacific-time days, and it reports a day only ~48h after it ends. Until then a day's change is
estimated from our daily follower-count snapshot (``growth_snapshots``, one per IST date, taken
late evening IST), which is a net change, can't be split into follows and unfollows, and lands
a few hours off Instagram's day: so it is shown apart and labelled approximate.

Per-reel follows are Instagram's lifetime count from the Business Suite export (``follows_import``);
the API has none for reels.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any


def day_labels(last_day: date, days: int) -> list[str]:
    first = last_day - timedelta(days=days - 1)
    return [(first + timedelta(days=i)).isoformat() for i in range(days)]


def series(
    days: Sequence[str],
    insights: Mapping[str, Mapping[str, Any]],
    counts: Mapping[str, int],
    still_following: Mapping[str, int],
) -> list[dict[str, Any]]:
    """One row per day: Instagram's follows/unfollows, the follower count, and an estimate where Instagram has none."""
    rows: list[dict[str, Any]] = []
    for day in days:
        row = insights.get(day) or {}
        follows, unfollows = row.get("follows"), row.get("unfollows")
        count = counts.get(day)
        before = counts.get((date.fromisoformat(day) - timedelta(days=1)).isoformat())
        estimate = count - before if follows is None and count is not None and before is not None else None
        rows.append(
            {
                "date": day,
                "follows": follows,
                "unfollows": unfollows,
                "followed_still_following": still_following.get(day, 0),
                "followers": count,
                # Only for a day Instagram hasn't reported: the follower count's change, net of unfollows.
                "net_estimate": estimate,
            }
        )
    return rows


def totals(rows: Sequence[Mapping[str, Any]], counts: Mapping[str, int]) -> dict[str, Any]:
    """The period in a few numbers. Follows and unfollows sum the days Instagram has reported."""
    reported = [r for r in rows if r.get("follows") is not None]
    pending = [r for r in rows if r.get("follows") is None]
    follows = sum(int(r["follows"] or 0) for r in reported)
    unfollows = sum(int(r.get("unfollows") or 0) for r in reported)
    best = max(reported, key=lambda r: int(r["follows"] or 0), default=None)
    known = [r["date"] for r in rows if r.get("followers") is not None]
    first_day = date.fromisoformat(rows[0]["date"]) if rows else None
    start = counts.get((first_day - timedelta(days=1)).isoformat()) if first_day else None
    return {
        "follows": follows,
        "unfollows": unfollows,
        "net": follows - unfollows,
        "days": len(rows),
        "days_reported": len(reported),
        "days_pending": [r["date"] for r in pending],
        "pending_net_estimate": sum(int(r["net_estimate"]) for r in pending if r.get("net_estimate") is not None),
        "best_day": {"date": best["date"], "follows": int(best["follows"] or 0)} if best else None,
        # The follower count just before the period and at its last known day, from our daily snapshots.
        "followers_start": start,
        "followers_end": counts.get(known[-1]) if known else None,
    }


def reel_rows(videos: Sequence[Mapping[str, Any]], iso: Any) -> list[dict[str, Any]]:
    """Reels with their follows (from Business Suite) and views, most follows first; ``None`` when not exported yet."""
    out: list[dict[str, Any]] = []
    for v in videos:
        meta = v.get("metadata") or {}
        follows, views = meta.get("follows"), meta.get("views")
        out.append(
            {
                "media_id": v.get("instagram_media_id"),
                "title": str(v.get("title") or "").split("\n")[0][:140],
                "published_at": iso(v.get("published_at")),
                "follows": follows,
                "follows_as_of": meta.get("follows_as_of"),
                "views": views,
                "follows_per_1k_views": round(follows * 1000 / views, 2) if follows is not None and views else None,
            }
        )
    out.sort(key=lambda r: (r["follows"] is None, -(r["follows"] or 0)))
    return out
