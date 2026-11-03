"""Numbers about a channel's DMs, worked out from the stored messages when asked.

Who started a conversation is only claimed when we hold its whole history (``history == "complete"``):
Instagram hides all but a conversation's 20 newest messages, so a longer old one reads as "started
before tracking" rather than a guess.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any

from app.services.dm_store import HISTORY_COMPLETE
from app.timezone import IST, assume_utc

WINDOWS: dict[str, int | None] = {"24h": 1, "7d": 7, "30d": 30, "all": None}
MAX_SERIES_DAYS = 30
REPLY_WINDOW = timedelta(hours=24)

STARTED_THEM = "them"
STARTED_US = "us"
STARTED_COMMENT_DM = "comment_dm"
STARTED_UNKNOWN = "unknown"


def _at(message: dict[str, Any]) -> datetime:
    return assume_utc(message["created_at"])


def _day(when: datetime) -> date:
    return when.astimezone(IST).date()


def started_by(conversation: dict[str, Any], messages: list[dict[str, Any]], comment_dm_ids: set[str]) -> str:
    """Who sent the first message: them, us by hand, us through comment-to-DM, or unknown."""
    if conversation.get("history") != HISTORY_COMPLETE or not messages:
        return STARTED_UNKNOWN
    first = messages[0]
    if not first["from_me"]:
        return STARTED_THEM
    return STARTED_COMMENT_DM if first["message_id"] in comment_dm_ids else STARTED_US


def _first_reply_minutes(messages: list[dict[str, Any]]) -> float | None:
    """Minutes from their first message to our first message after it."""
    theirs = next((m for m in messages if not m["from_me"]), None)
    if theirs is None:
        return None
    ours = next((m for m in messages if m["from_me"] and _at(m) >= _at(theirs)), None)
    return (_at(ours) - _at(theirs)).total_seconds() / 60 if ours else None


def dm_stats(
    conversations: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    *,
    comment_dm_ids: set[str],
    commenter_ids: set[str],
    now: datetime,
    days: int | None,
) -> dict[str, Any]:
    """Conversations started by them vs us, replies both ways, who's waiting, and a daily series."""
    by_peer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in messages:
        if not m.get("deleted"):
            by_peer[m["peer_id"]].append(m)
    for thread in by_peer.values():
        thread.sort(key=_at)
    start = now - timedelta(days=days) if days else None

    def in_window(when: datetime) -> bool:
        return start is None or when >= start

    started: dict[str, int] = defaultdict(int)
    new_started: dict[str, int] = defaultdict(int)
    active = messaged_us = from_commenters = awaiting = awaiting_open = 0
    we_replied = them_started = comment_dm_started = comment_dm_answered = 0
    reply_minutes: list[float] = []
    series_new: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for conv in conversations:
        thread = by_peer.get(conv["peer_id"], [])
        if not thread:
            continue
        who = started_by(conv, thread, comment_dm_ids)
        started[who] += 1
        if thread[-1]["from_me"] is False:
            awaiting += 1
            awaiting_open += now - _at(thread[-1]) < REPLY_WINDOW
        if who != STARTED_UNKNOWN:
            series_new[_day(_at(thread[0]))][who] += 1
        window_msgs = [m for m in thread if in_window(_at(m))]
        if not window_msgs:
            continue
        active += 1
        if any(not m["from_me"] for m in window_msgs):
            messaged_us += 1
            from_commenters += conv["peer_id"] in commenter_ids
        if who == STARTED_UNKNOWN or not in_window(_at(thread[0])):
            continue
        new_started[who] += 1
        if who == STARTED_THEM:
            them_started += 1
            minutes = _first_reply_minutes(thread)
            if minutes is not None:
                we_replied += 1
                reply_minutes.append(minutes)
        elif who == STARTED_COMMENT_DM:
            comment_dm_started += 1
            comment_dm_answered += any(not m["from_me"] for m in thread)

    window_all = [m for m in messages if not m.get("deleted") and in_window(_at(m))]
    series_days = min(days or MAX_SERIES_DAYS, MAX_SERIES_DAYS)
    today = _day(now)
    first_day = today - timedelta(days=series_days - 1)
    msgs_in: dict[date, int] = defaultdict(int)
    for m in messages:
        if not m["from_me"] and not m.get("deleted") and _day(_at(m)) >= first_day:
            msgs_in[_day(_at(m))] += 1
    series = [
        {
            "date": d.isoformat(),
            "started_by_them": series_new[d][STARTED_THEM],
            "started_by_us": series_new[d][STARTED_US] + series_new[d][STARTED_COMMENT_DM],
            "messages_in": msgs_in[d],
        }
        for d in (first_day + timedelta(days=i) for i in range(series_days))
    ]

    def rate(part: int, whole: int) -> float | None:
        return round(part / whole, 4) if whole else None

    return {
        "totals": {
            "conversations": active,
            "accounts_messaged": messaged_us,
            "messages_in": sum(not m["from_me"] for m in window_all),
            "messages_out": sum(m["from_me"] for m in window_all),
            "new_by_them": new_started[STARTED_THEM],
            "new_by_us": new_started[STARTED_US],
            "new_by_comment_dm": new_started[STARTED_COMMENT_DM],
            "reply_rate": rate(we_replied, them_started),
            "median_reply_minutes": round(median(reply_minutes), 1) if reply_minutes else None,
            "comment_dm_reply_rate": rate(comment_dm_answered, comment_dm_started),
            "from_commenters": from_commenters,
        },
        # Every stored conversation, whatever the window: who started each.
        "started_all_time": {
            key: started[key] for key in (STARTED_THEM, STARTED_US, STARTED_COMMENT_DM, STARTED_UNKNOWN)
        },
        "awaiting_reply": awaiting,
        "awaiting_reply_open": awaiting_open,
        "series": series,
        "tracked_since": min((_at(m) for m in messages), default=now).astimezone(IST).isoformat() if messages else None,
    }
