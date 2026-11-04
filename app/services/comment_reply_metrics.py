"""Headline numbers for a channel's comment replies: what came in, what was answered, and how.

Two collections hold the story:

- ``comment_ai_reply_queue``: one row per Instagram comment the webhook delivered, with why it was
  skipped (our own comment, a reply inside a thread, spam, ...) or that it was answered.
- ``comment_replies``: one row per comment the reply logic decided on, from the webhook worker or
  the scheduled cycle: replied, skipped by sentiment, a draft waiting for approval, or rejected.

Everything here is pure; the router reads the rows in the window and hands them over.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from app.timezone import IST, assume_utc

REPLIED = "replied"
PENDING = "pending_approval"
REJECTED = "rejected"
OWN_COMMENT = "our own comment"
SERIES_DAYS = 14
TOP_REELS = 5

# comment_replies statuses that mean "decided not to answer", with the label the queue would use.
_SKIP_STATUS = {
    "skipped_spam": "spam comment",
    "skipped_neutral": "neutral comment",
    "skipped_negative": "negative comment",
    "skipped_creepy": "creepy comment",
    "skipped_limit": "reply limit for this person",
}


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return assume_utc(value)
    if isinstance(value, str) and value:
        try:
            return assume_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _in(value: Any, since: datetime | None) -> bool:
    at = _when(value)
    return at is not None and (since is None or at >= since)


def reply_metrics(
    replies: Iterable[Mapping[str, Any]],
    queue: Iterable[Mapping[str, Any]],
    now: datetime,
    days: int | None,
) -> dict[str, Any]:
    """The metrics for the last *days* days (``None`` for all time), plus a 14-day daily series.

    *replies* are ``comment_replies`` rows and *queue* ``comment_ai_reply_queue`` rows for one
    channel; pass at least the last 14 days of each so the series is complete.
    """
    replies = list(replies)
    queue = list(queue)
    since = now - timedelta(days=days) if days else None
    window_replies = [r for r in replies if _in(r.get("replied_at"), since)]
    window_queue = [q for q in queue if _in(q.get("created_at"), since)]

    # Comments in: the webhook's intake when the channel has one (minus our own comments), else the
    # rows the scheduled cycle looked at.
    if queue:
        incoming = [q for q in window_queue if q.get("skip_reason") != OWN_COMMENT]
        comments_in = len(incoming)
        skip_reasons = Counter(
            q["skip_reason"] for q in incoming if q.get("status") == "skipped" and q.get("skip_reason")
        )
        failed = sum(1 for q in incoming if q.get("status") == "failed")
        own = len(window_queue) - comments_in
    else:
        comments_in = len(window_replies)
        skip_reasons = Counter(
            _SKIP_STATUS.get(r.get("status", ""), r.get("status", ""))
            for r in window_replies
            if str(r.get("status", "")).startswith("skipped")
        )
        failed = 0
        own = 0
    failed += sum(1 for r in window_replies if r.get("status") == PENDING and r.get("last_error"))

    replied = [r for r in window_replies if r.get("status") == REPLIED]
    approved = [r for r in replied if r.get("approved")]
    sentiment = Counter(r.get("sentiment") or "unknown" for r in window_replies)

    commented_at = {str(q.get("comment_id")): q.get("commented_at") for q in queue}
    waits = []
    for r in replied:
        start = _when(commented_at.get(str(r.get("comment_id")))) or _when(r.get("comment_published_at"))
        end = _when(r.get("replied_at"))
        if start and end and end >= start:
            waits.append((end - start).total_seconds() / 60)

    reels: Counter[str] = Counter()
    reels_replied: Counter[str] = Counter()
    for r in window_replies:
        title = r.get("video_title") or "Untitled reel"
        reels[title] += 1
        if r.get("status") == REPLIED:
            reels_replied[title] += 1

    return {
        "days": days,
        "comments_in": comments_in,
        "own_comments": own,
        "replied": len(replied),
        "reply_rate": round(len(replied) / comments_in, 3) if comments_in else None,
        "auto_replied": len(replied) - len(approved),
        "approved": len(approved),
        "edited": sum(1 for r in approved if r.get("edited")),
        "rejected": sum(1 for r in window_replies if r.get("status") == REJECTED),
        "pending_now": sum(1 for r in replies if r.get("status") == PENDING),
        "failed": failed,
        "median_reply_minutes": round(median(waits), 1) if waits else None,
        "skipped": [{"reason": k, "count": v} for k, v in skip_reasons.most_common()],
        "sentiment": [{"sentiment": k, "count": v} for k, v in sentiment.most_common()],
        "top_reels": [
            {"title": t, "comments": n, "replied": reels_replied[t]} for t, n in reels.most_common(TOP_REELS)
        ],
        "series": daily_series(replies, queue, now),
    }


def daily_series(
    replies: Iterable[Mapping[str, Any]], queue: Iterable[Mapping[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    """Comments in and replies sent per IST day, for the last 14 days, oldest first."""
    queue = list(queue)
    today = now.astimezone(IST).date()
    days = [today - timedelta(days=n) for n in range(SERIES_DAYS - 1, -1, -1)]
    comments: Counter[str] = Counter()
    sent: Counter[str] = Counter()
    for q in queue:
        at = _when(q.get("created_at"))
        if at and q.get("skip_reason") != OWN_COMMENT:
            comments[at.astimezone(IST).date().isoformat()] += 1
    for r in replies:
        at = _when(r.get("replied_at"))
        if not at:
            continue
        day = at.astimezone(IST).date().isoformat()
        if not queue:
            comments[day] += 1
        if r.get("status") == REPLIED:
            sent[day] += 1
    return [{"date": d.isoformat(), "comments": comments[d.isoformat()], "replied": sent[d.isoformat()]} for d in days]
