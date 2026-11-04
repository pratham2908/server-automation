"""Comment-to-DM numbers: buckets, the after-the-DM funnel, and the follow check that feeds it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient

from app.main import app
from app.routers import comment_dm as router_mod
from app.services import follow_status
from app.services.comment_dm_numbers import numbers
from tests.conftest import GLOBAL_API_KEY
from tests.mongo_fakes import DB

# 12:30 IST on 4 Nov, as Mongo hands it back (naive UTC).
NOW = datetime(2026, 11, 4, 7, 0, tzinfo=UTC)


def _ev(minutes_ago: float, status: str, person: str = "p1", media: str = "m1", wait: float = 20) -> dict[str, Any]:
    created = (NOW - timedelta(minutes=minutes_ago)).replace(tzinfo=None)
    row: dict[str, Any] = {
        "status": status,
        "created_at": created,
        "commented_at": created,
        "from_id": person,
        "media_id": media,
    }
    if status == "sent":
        row["sent_at"] = created + timedelta(seconds=wait)
    return row


def _numbers(events: list[dict[str, Any]], window: str = "24h", **kw: Any):
    base: dict[str, Any] = {"replies_by_person": {}, "taps_by_person": {}, "follows_by_person": {}}
    return numbers(events, window=window, now=NOW, **{**base, **kw})


def test_a_day_is_24_hourly_buckets_and_every_status_lands_in_its_own() -> None:
    result = _numbers(
        [_ev(5, "sent"), _ev(6, "failed", "p2"), _ev(7, "skipped"), _ev(8, "pending", "p3"), _ev(200, "sent", "p4")]
    )
    assert result.bucket == "hour" and len(result.points) == 25  # the partial hour 24 h ago, through now
    last = result.points[-1]
    assert (last.sent, last.failed, last.skipped, last.queued) == (1, 1, 1, 1)
    assert sum(p.sent for p in result.points) == 2 and result.points[-1].at.endswith("+05:30")


def test_older_rows_fall_outside_the_window() -> None:
    result = _numbers([_ev(5, "sent"), _ev(60 * 30, "sent", "p2")])
    assert result.funnel.comments == 1 and result.funnel.dm_people == 1


def test_a_week_is_daily_and_all_time_starts_at_the_first_comment() -> None:
    week = _numbers([_ev(5, "sent")], window="7d")
    assert week.bucket == "day" and len(week.points) == 8
    whole = _numbers([_ev(5, "sent"), _ev(60 * 24 * 3, "sent", "p2")], window="all")
    assert len(whole.points) == 4


def test_the_funnel_counts_people_and_only_what_came_after_their_dm() -> None:
    events = [
        _ev(30, "sent", "a"),
        _ev(29, "skipped", "a"),
        _ev(20, "sent", "b"),
        _ev(10, "sent", "c"),
        _ev(5, "failed", "d"),
    ]
    sent_a = NOW - timedelta(minutes=30) + timedelta(seconds=20)
    result = _numbers(
        events,
        replies_by_person={"a": [sent_a + timedelta(minutes=1)], "b": [NOW - timedelta(days=2)]},
        taps_by_person={"a": [sent_a + timedelta(minutes=2)]},
        follows_by_person={"a": True, "b": False, "c": None},
    )
    f = result.funnel
    assert (f.comments, f.people, f.dm_people) == (5, 4, 3)
    # b wrote two days BEFORE the DM: that's not an answer to it.
    assert (f.wrote_back, f.tapped) == (1, 1)
    assert (f.follow_known, f.following) == (2, 1)
    assert result.median_seconds_to_dm == 20 and result.fail_rate == pytest.approx(1 / 4)


def test_reels_are_ranked_by_comments() -> None:
    result = _numbers([_ev(5, "sent", "a", "m2"), _ev(6, "sent", "b", "m2"), _ev(7, "failed", "c", "m1")])
    assert [(r.media_id, r.comments, r.sent, r.failed) for r in result.reels] == [("m2", 2, 2, 0), ("m1", 1, 0, 1)]


def test_nothing_is_no_rates_not_zero_rates() -> None:
    result = _numbers([])
    assert result.median_seconds_to_dm is None and result.fail_rate is None and result.funnel.comments == 0


class _Profiles:
    provider = "instagram"

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def messaging_profile(self, igsid: str) -> dict[str, Any]:
        self.asked.append(igsid)
        answer = self.answers[igsid]
        if isinstance(answer, Exception):
            raise answer
        return {"is_user_follow_business": answer}


def _no_consent() -> requests.HTTPError:
    response = requests.Response()
    response.status_code = 400
    response._content = b'{"error": {"message": "User consent is required", "code": 230}}'
    return requests.HTTPError("400", response=response)


@pytest.mark.asyncio
async def test_follows_are_asked_only_of_people_who_wrote_and_kept_on_their_conversation() -> None:
    now = NOW
    db = DB(
        dm_messages=[
            {"channel_id": "ch", "peer_id": "a", "from_me": False},
            {"channel_id": "ch", "peer_id": "b", "from_me": False},
            {"channel_id": "ch", "peer_id": "c", "from_me": False},
            {"channel_id": "ch", "peer_id": "silent", "from_me": True},
        ],
        dm_conversations=[{"channel_id": "ch", "peer_id": p} for p in ("a", "b", "c", "silent")]
        + [{"channel_id": "ch", "peer_id": "fresh", "follows_us": True, "follows_checked_at": now}],
    )
    db.dm_messages.docs.append({"channel_id": "ch", "peer_id": "fresh", "from_me": False})
    service = _Profiles({"a": True, "b": False, "c": _no_consent()})
    counts = await follow_status.refresh_channel(db, "ch", service, now, spacing=0)
    assert sorted(service.asked) == ["a", "b", "c"]  # never "silent" (only we wrote), never "fresh" (asked today)
    assert counts == {"checked": 3, "following": 1, "not_following": 1, "unknown": 1}
    by_peer = {c["peer_id"]: c.get("follows_us") for c in db.dm_conversations.docs}
    assert by_peer == {"a": True, "b": False, "c": None, "silent": None, "fresh": True}


def test_the_numbers_route_reads_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _ev(5, "sent", "a")
    db = DB(
        channels=[{"channel_id": "ch", "platform": "instagram"}],
        comment_dm_events=[{**sent, "channel_id": "ch"}, {**_ev(6, "failed", "b"), "channel_id": "ch"}],
        dm_messages=[
            {"channel_id": "ch", "peer_id": "a", "from_me": False, "created_at": sent["sent_at"] + timedelta(minutes=1)}
        ],
        dm_conversations=[{"channel_id": "ch", "peer_id": "a", "follows_us": True}],
        videos=[{"channel_id": "ch", "instagram_media_id": "m1", "title": "BFS"}],
    )
    app.dependency_overrides[router_mod.get_db] = lambda: db
    try:
        monkeypatch.setattr(router_mod, "now_ist", lambda: NOW)
        body = (
            TestClient(app)
            .get("/api/v1/channels/ch/comment-dm/numbers?window=24h", headers={"x-api-key": GLOBAL_API_KEY})
            .json()
        )
    finally:
        app.dependency_overrides.pop(router_mod.get_db, None)
    assert body["funnel"]["dm_people"] == 1 and body["funnel"]["wrote_back"] == 1 and body["funnel"]["following"] == 1
    assert body["reels"][0]["title"] == "BFS" and body["fail_rate"] == 0.5
