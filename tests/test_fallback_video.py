"""Fallback videos: rule checks, the hourly cron, and the API that sets and removes a rule."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.services import fallback_video_cron as cron
from app.services.auto_scheduler_selection import pick_ready_video
from app.services.fallback_video import FAILED, MET, PENDING, TRIGGERED, FallbackRule, rule_fields
from app.services.trial_graduation_cron import Snapshot
from tests.mongo_fakes import DB

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
CHECK = {"check_until_hours": 6, "groups": [{"conditions": [{"metric": "views", "op": "gte", "value": 1000}]}]}


def _rule(fallback_id: str = "fb") -> FallbackRule:
    return FallbackRule.model_validate({"fallback_video_id": fallback_id, "check": CHECK})


def _fallback(**extra: Any) -> dict[str, Any]:
    return {"_id": "fb", "channel_id": "c", "video_id": "fb", "status": "ready", "r2_object_key": "k", **extra}


def _reel(**extra: Any) -> dict[str, Any]:
    return {"_id": "r", "channel_id": "c", "video_id": "r", "status": "queued", "title": "Reel", **extra}


# ---- rule_fields ----------------------------------------------------------------------------------


def test_a_scheduled_reel_waits_for_publish_before_its_first_check():
    fields = rule_fields(_reel(), _fallback(), _rule(), "instagram", NOW)
    assert fields["fallback"] == {"video_id": "fb", "check": _rule().check.model_dump()}
    assert fields["fallback_state"] == PENDING and fields["fallback_due_at"] is None


def test_a_posted_reel_is_checked_at_its_next_hourly_slot_or_now():
    fresh = _reel(status="published", published_at=NOW - timedelta(minutes=20))
    assert rule_fields(fresh, _fallback(), _rule(), "instagram", NOW)["fallback_due_at"] == NOW + timedelta(minutes=40)
    older = _reel(status="published", published_at=NOW - timedelta(hours=3))
    assert rule_fields(older, _fallback(), _rule(), "instagram", NOW)["fallback_due_at"] == NOW


@pytest.mark.parametrize(
    ("reel", "fallback", "platform", "needle"),
    [
        (_reel(), _fallback(), "youtube", "Instagram"),
        (_reel(), _fallback(status="published"), "instagram", "ready video"),
        (_reel(), _fallback(r2_object_key=None), "instagram", "no uploaded file"),
        (_reel(), _fallback(fallback_for="other"), "instagram", "another reel"),
        (_reel(), None, "instagram", "not found"),
        (_reel(fallback_state=TRIGGERED), _fallback(), "instagram", "already finished"),
        (_reel(status="published", published_at=NOW - timedelta(hours=7)), _fallback(), "instagram", "up 7h"),
        (_reel(video_id="fb"), _fallback(), "instagram", "its own fallback"),
    ],
)
def test_rules_that_cannot_work_are_refused(reel, fallback, platform, needle):
    with pytest.raises(ValueError, match=needle):
        rule_fields(reel, fallback, _rule(), platform, NOW)


def test_the_auto_scheduler_skips_a_reserved_fallback():
    old = {"video_id": "a", "status": "ready", "created_at": NOW - timedelta(days=2), "fallback_for": "r"}
    new = {"video_id": "b", "status": "ready", "created_at": NOW}
    assert pick_ready_video([old, new])["video_id"] == "b"


# ---- the cron -------------------------------------------------------------------------------------


class _Harness:
    def __init__(self, reel: dict[str, Any], views: float | None = 10, fallback: dict[str, Any] | None = None) -> None:
        self.db = DB(videos=[reel, fallback or _fallback(fallback_for="r")])
        self.views = views
        self.published: list[dict[str, Any]] = []
        self.emails: list[tuple[str, str]] = []

    async def fetch(self, channel_id: str, media_id: str) -> Snapshot:
        if self.views is None:
            raise RuntimeError("Graph down")
        return Snapshot(permalink="https://ig/r", metrics={"views": self.views})

    async def send(self, subject: str, text: str, html: str) -> bool:
        self.emails.append((subject, text))
        return True

    async def publish(self, **kw: Any) -> dict[str, Any]:
        self.published.append(kw)
        return {"status": "queued"}

    async def run(self, now: datetime = NOW) -> dict[str, int]:
        return await cron.run_fallback_checks(self.db, self.fetch, self.send, now=now, publish=self.publish)

    async def doc(self, video_id: str) -> dict[str, Any]:
        return await self.db.videos.find_one({"video_id": video_id})


def _live(**extra: Any) -> dict[str, Any]:
    rule = {"fallback": {"video_id": "fb", "check": CHECK}, "fallback_state": PENDING, "fallback_due_at": None}
    base = {"status": "published", "instagram_media_id": "m1", "published_at": NOW - timedelta(hours=1), **rule}
    return _reel(**{**base, **extra})


@pytest.mark.asyncio
async def test_a_reel_that_publishes_gets_its_first_check_time():
    h = _Harness(_live(published_at=NOW - timedelta(minutes=10)))
    stats = await h.run()
    assert stats["armed"] == 1 and stats["checked"] == 0
    assert (await h.doc("r"))["fallback_due_at"] == NOW + timedelta(minutes=50)


@pytest.mark.asyncio
async def test_clearing_the_bar_ends_the_check_and_frees_the_fallback():
    h = _Harness(_live(fallback_due_at=NOW), views=1500)
    await h.run()
    reel = await h.doc("r")
    assert reel["fallback_state"] == MET and reel["fallback_met_after_hours"] == 1
    assert (await h.doc("fb"))["fallback_for"] is None
    assert h.published == [] and h.emails == []


@pytest.mark.asyncio
async def test_short_of_the_bar_inside_the_window_checks_again_in_an_hour():
    h = _Harness(_live(fallback_due_at=NOW), views=200)
    await h.run()
    reel = await h.doc("r")
    assert reel["fallback_state"] == PENDING and reel["fallback_due_at"] == NOW + timedelta(hours=1)
    assert reel["fallback_metrics"] == {"views": 200}
    assert h.published == []


@pytest.mark.asyncio
async def test_missing_the_bar_at_the_end_publishes_the_fallback_and_emails():
    h = _Harness(_live(published_at=NOW - timedelta(hours=6), fallback_due_at=NOW), views=200)
    await h.run()
    reel = await h.doc("r")
    assert reel["fallback_state"] == TRIGGERED and reel["fallback_publish_at"] == NOW + timedelta(minutes=2)
    assert [(p["video_doc"]["video_id"], p["scheduled_at"], p["platform"]) for p in h.published] == [
        ("fb", NOW + timedelta(minutes=2), "instagram")
    ]
    assert len(h.emails) == 1 and "missed its bar" in h.emails[0][0] and "views 200" in h.emails[0][1]
    await h.run()  # the email is owed once
    assert len(h.emails) == 1 and len(h.published) == 1


@pytest.mark.asyncio
async def test_a_fallback_that_was_scheduled_elsewhere_is_not_published_twice():
    reel = _live(published_at=NOW - timedelta(hours=6), fallback_due_at=NOW)
    h = _Harness(reel, views=200, fallback=_fallback(status="queued", fallback_for="r"))
    await h.run()
    doc = await h.doc("r")
    assert doc["fallback_state"] == FAILED and "'queued', not ready" in doc["fallback_error"]
    assert h.published == [] and "not published" in h.emails[0][0]


@pytest.mark.asyncio
async def test_reading_the_reel_failing_for_an_hour_gives_up(monkeypatch):
    async def quiet(**_kw: Any) -> None:
        return None

    monkeypatch.setattr(cron, "report_error", quiet)
    h = _Harness(_live(fallback_due_at=NOW, fallback_attempts=cron.MAX_FETCH_ATTEMPTS - 1), views=None)
    await h.run()
    doc = await h.doc("r")
    assert doc["fallback_state"] == FAILED and "Graph down" in doc["fallback_error"]
    assert (await h.doc("fb"))["fallback_for"] is None


# ---- the API --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_change_and_remove_a_rule(monkeypatch):
    from app.routers import fallback_video as api

    monkeypatch.setattr(api, "now_ist", lambda: NOW)
    db = DB(
        channels=[{"channel_id": "c", "platform": "instagram"}],
        videos=[_reel(), _fallback(), _fallback(_id="fb2", video_id="fb2")],
    )
    out = await api.set_fallback("c", "r", _rule("fb"), db)
    assert out.state == PENDING and out.fallback.video_id == "fb" and out.due_at is None
    assert (await db.videos.find_one({"video_id": "fb"}))["fallback_for"] == "r"

    await api.set_fallback("c", "r", _rule("fb2"), db)
    assert (await db.videos.find_one({"video_id": "fb"}))["fallback_for"] is None
    assert (await db.videos.find_one({"video_id": "fb2"}))["fallback_for"] == "r"

    listed = await api.list_fallbacks("c", db)
    assert [f.fallback.video_id for f in listed.fallbacks] == ["fb2"]

    assert (await api.remove_fallback("c", "r", db))["deleted"] is True
    assert (await db.videos.find_one({"video_id": "fb2"}))["fallback_for"] is None
    assert "fallback" not in await db.videos.find_one({"video_id": "r"})


@pytest.mark.asyncio
async def test_a_refused_rule_is_a_422(monkeypatch):
    from fastapi import HTTPException

    from app.routers import fallback_video as api

    db = DB(channels=[{"channel_id": "c", "platform": "instagram"}], videos=[_reel(), _fallback(status="queued")])
    with pytest.raises(HTTPException) as err:
        await api.set_fallback("c", "r", _rule("fb"), db)
    assert err.value.status_code == 422 and "ready video" in err.value.detail
