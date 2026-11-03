"""Who comments on a channel: per-account counts worked out from ``comment_log`` when they're asked for.

Nothing is pre-aggregated. The log gains a row the moment a comment arrives, so these numbers are live,
and any window can be asked for. The channel's own comments never reach here.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from app.timezone import IST, assume_utc

WINDOWS: dict[str, int | None] = {"7d": 7, "30d": 30, "90d": 90, "all": None}
TOP_LIMIT = 50
MAX_SERIES_DAYS = 30
# How many accounts the "top accounts' share" tile measures.
TOP_SHARE_N = 10
DETAIL_LIMIT = 200

# Accounts grouped by how many comments they left in the window: label, lowest count, highest (None = no cap).
BUCKETS: list[tuple[str, int, int | None]] = [
    ("1", 1, 1),
    ("2", 2, 2),
    ("3–5", 3, 5),
    ("6–10", 6, 10),
    ("11+", 11, None),
]


def _at(row: dict[str, Any]) -> datetime:
    return assume_utc(row["commented_at"])


def _day(when: datetime) -> date:
    return when.astimezone(IST).date()


def _iso(when: datetime | None) -> str | None:
    return when.astimezone(IST).isoformat() if when else None


def _accounts(rows: list[dict[str, Any]], first_seen: dict[str, datetime], start: datetime | None) -> list[dict]:
    """One entry per author in *rows*, oldest row first so the newest wins the handle and last text."""
    by_author: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=_at):
        author = row["author"]
        acc = by_author.setdefault(
            author,
            {"author": author, "comments": 0, "top_level": 0, "replies": 0, "media": set(), "first_at": _at(row)},
        )
        acc["comments"] += 1
        acc["replies" if row.get("is_reply") else "top_level"] += 1
        acc["media"].add(row["media_id"])
        acc["last_at"] = _at(row)
        acc["username"] = row.get("username") or author
        acc["last_text"] = row.get("text") or ""
    out = []
    for acc in by_author.values():
        media = acc.pop("media")
        first_ever = first_seen[acc["author"]]
        out.append(
            {
                **acc,
                "videos": len(media),
                "first_seen_at": first_ever,
                "is_new": start is not None and first_ever >= start,
            }
        )
    return out


def _series(
    rows: list[dict[str, Any]], first_seen: dict[str, datetime], today: date, days: int
) -> list[dict[str, Any]]:
    start = today - timedelta(days=days - 1)
    comments: dict[date, int] = defaultdict(int)
    authors: dict[date, set[str]] = defaultdict(set)
    for row in rows:
        day = _day(_at(row))
        if day >= start:
            comments[day] += 1
            authors[day].add(row["author"])
    new: dict[date, int] = defaultdict(int)
    for when in first_seen.values():
        new[_day(when)] += 1
    return [
        {"date": d.isoformat(), "comments": comments[d], "accounts": len(authors[d]), "new_accounts": new[d]}
        for d in (start + timedelta(days=i) for i in range(days))
    ]


def _share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def commenter_stats(rows: list[dict[str, Any]], now: datetime, days: int | None, top: int = TOP_LIMIT) -> dict:
    """Totals, top accounts, how comment counts spread across accounts, and a daily series.

    *rows* are every viewer comment the channel has logged (own comments already left out); *days*
    is the window, None for all time. "New" means the account's first logged comment falls in the window.
    """
    first_seen: dict[str, datetime] = {}
    for row in rows:
        at = _at(row)
        if row["author"] not in first_seen or at < first_seen[row["author"]]:
            first_seen[row["author"]] = at
    start = now - timedelta(days=days) if days else None
    window = [r for r in rows if start is None or _at(r) >= start]
    accounts = _accounts(window, first_seen, start)
    accounts.sort(key=lambda a: (a["comments"], a["videos"], a["last_at"]), reverse=True)

    comments = len(window)
    repeat = [a for a in accounts if a["comments"] >= 2]
    replies = sum(a["replies"] for a in accounts)
    totals = {
        "comments": comments,
        "top_level": comments - replies,
        "replies": replies,
        "accounts": len(accounts),
        "videos": len({r["media_id"] for r in window}),
        "new_accounts": sum(a["is_new"] for a in accounts) if start else len(accounts),
        "repeat_accounts": len(repeat),
        "multi_video_accounts": sum(a["videos"] >= 2 for a in accounts),
        "repeat_share": _share(sum(a["comments"] for a in repeat), comments),
        "top_share": _share(sum(a["comments"] for a in accounts[:TOP_SHARE_N]), comments),
        "per_account": round(comments / len(accounts), 2) if accounts else 0.0,
    }
    distribution = [
        {"label": label, "accounts": sum(lo <= a["comments"] and (hi is None or a["comments"] <= hi) for a in accounts)}
        for label, lo, hi in BUCKETS
    ]
    series_days = min(days or MAX_SERIES_DAYS, MAX_SERIES_DAYS)
    return {
        "totals": totals,
        "top_share_n": TOP_SHARE_N,
        "top": [
            {
                **a,
                "first_at": _iso(a["first_at"]),
                "last_at": _iso(a["last_at"]),
                "first_seen_at": _iso(a["first_seen_at"]),
            }
            for a in accounts[:top]
        ],
        "distribution": distribution,
        "series": _series(rows, first_seen, _day(now), series_days),
        "tracked_since": _iso(min(first_seen.values())) if first_seen else None,
    }


def account_detail(rows: list[dict[str, Any]], titles: dict[str, str]) -> dict[str, Any] | None:
    """One account's whole history on the channel: totals and their comments, newest first.

    *titles* names the reels we know (media id → title); a post published outside the app has none.
    """
    if not rows:
        return None
    rows = sorted(rows, key=_at, reverse=True)
    replies = sum(bool(r.get("is_reply")) for r in rows)
    return {
        "author": rows[0]["author"],
        "username": rows[0].get("username") or rows[0]["author"],
        "comments": len(rows),
        "top_level": len(rows) - replies,
        "replies": replies,
        "videos": len({r["media_id"] for r in rows}),
        "first_at": _iso(_at(rows[-1])),
        "last_at": _iso(_at(rows[0])),
        "items": [
            {
                "comment_id": r["comment_id"],
                "text": r.get("text") or "",
                "is_reply": bool(r.get("is_reply")),
                "media_id": r["media_id"],
                "video_title": titles.get(r["media_id"], ""),
                "commented_at": _iso(_at(r)),
            }
            for r in rows[:DETAIL_LIMIT]
        ],
    }
