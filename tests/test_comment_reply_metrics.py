"""Metrics for the Auto-replies & review queue tab."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.services.comment_reply_metrics import reply_metrics
from tests.mongo_fakes import DB

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _q(cid: str, status: str, reason: str | None = None, ago: timedelta = timedelta(hours=2)) -> dict[str, Any]:
    at = (NOW - ago).replace(tzinfo=None)
    return {"comment_id": cid, "status": status, "skip_reason": reason, "created_at": at, "commented_at": at}


def _r(cid: str, status: str, sentiment: str, ago: timedelta = timedelta(hours=1), **extra: Any) -> dict[str, Any]:
    return {
        "comment_id": cid,
        "status": status,
        "sentiment": sentiment,
        "replied_at": (NOW - ago).replace(tzinfo=None),
        "video_title": extra.pop("title", "Reel A"),
        **extra,
    }


QUEUE = [
    _q("1", "done"),
    _q("2", "done"),
    _q("3", "skipped", "spam comment"),
    _q("4", "skipped", "a reply inside a thread"),
    _q("5", "skipped", "our own comment"),
    _q("6", "failed"),
    _q("old", "done", ago=timedelta(days=10)),
]
REPLIES = [
    _r("1", "replied", "positive"),  # an hour after the comment
    _r("2", "replied", "positive", approved=True, edited=True, title="Reel B"),
    _r("3", "skipped_spam", "spam"),
    _r("7", "pending_approval", "neutral"),
    _r("8", "rejected", "neutral"),
    _r("old", "replied", "positive", ago=timedelta(days=9)),
]


def test_the_week_counts_intake_answers_and_skips():
    m = reply_metrics(REPLIES, QUEUE, NOW, 7)
    assert m["comments_in"] == 5  # our own comment is left out
    assert m["own_comments"] == 1
    assert (m["replied"], m["auto_replied"], m["approved"], m["edited"]) == (2, 1, 1, 1)
    assert m["reply_rate"] == 0.4
    assert (m["rejected"], m["pending_now"], m["failed"]) == (1, 1, 1)
    assert m["skipped"] == [{"reason": "spam comment", "count": 1}, {"reason": "a reply inside a thread", "count": 1}]
    assert m["median_reply_minutes"] == 60.0
    assert m["top_reels"][0] == {"title": "Reel A", "comments": 4, "replied": 1}


def test_all_time_includes_older_rows_and_the_series_spans_14_days():
    m = reply_metrics(REPLIES, QUEUE, NOW, None)
    assert m["replied"] == 3 and m["comments_in"] == 6
    assert len(m["series"]) == 14 and m["series"][-1] == {"date": "2026-11-02", "comments": 5, "replied": 2}


def test_a_channel_without_the_webhook_counts_the_cycles_rows():
    m = reply_metrics(REPLIES, [], NOW, 7)
    assert m["comments_in"] == 5
    assert m["skipped"] == [{"reason": "spam comment", "count": 1}]
    assert m["median_reply_minutes"] is None


def test_nothing_yet_is_zeroes_not_errors():
    m = reply_metrics([], [], NOW, 1)
    assert m["comments_in"] == 0 and m["reply_rate"] is None and m["top_reels"] == []


@pytest.mark.asyncio
async def test_the_endpoint_reads_the_window(monkeypatch):
    from app.routers import comment_reply_metrics as api

    monkeypatch.setattr(api, "now_ist", lambda: NOW)
    db = DB(
        comment_replies=[{"channel_id": "z", **r} for r in REPLIES],
        comment_ai_reply_queue=[{"channel_id": "z", **q} for q in QUEUE],
    )
    out = await api.comment_reply_metrics("z", "24h", db)
    assert out["window"] == "24h" and out["replied"] == 2 and out["pending_now"] == 1
