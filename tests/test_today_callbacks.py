"""Today's video by callback: the app calls us once instead of being polled.

Covers the password (hash-only storage, single use, constant-time check), the
offer lifecycle in the service (kept only when the app accepts it), the scheduler
(no polling while a callback is promised, one last ask at the deadline), and the
HTTP route's answers.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.models.today_callback import TodayCallbackBody
from app.routers import source_callbacks as route
from app.services import auto_scheduler_cron as cron
from app.services import today_callbacks as cb
from app.services import video_source_service as vss
from app.services.video_sources import TodaysVideo
from app.timezone import IST

DAY = date(2026, 8, 24)


def _dt(h: int, mi: int = 0) -> datetime:
    return datetime(2026, 8, 24, h, mi, tzinfo=IST)


SLOT = cb.CallbackSlot(channel_id="histriphy", day=DAY, slot="19:00", public_base_url="https://us.example/")


# ------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------


def _matches(doc: dict, query: dict) -> bool:
    for key, want in query.items():
        have = doc.get(key)
        if isinstance(want, dict) and "$lte" in want:
            if have is None or have > want["$lte"]:
                return False
        elif have != want:
            return False
    return True


class _Result(SimpleNamespace):
    pass


class FakeCallbacks:
    def __init__(self) -> None:
        self.docs: list[dict] = []

    async def insert_one(self, doc: dict) -> None:
        self.docs.append(copy.deepcopy(doc))

    async def find_one(self, query: dict) -> dict | None:
        return next((copy.deepcopy(d) for d in self.docs if _matches(d, query)), None)

    async def update_one(self, query: dict, update: dict) -> _Result:
        for d in self.docs:
            if _matches(d, query):
                d.update(update["$set"])
                return _Result(modified_count=1)
        return _Result(modified_count=0)

    async def update_many(self, query: dict, update: dict) -> _Result:
        hits = [d for d in self.docs if _matches(d, query)]
        for d in hits:
            d.update(update["$set"])
        return _Result(modified_count=len(hits))

    async def find_one_and_update(self, query: dict, update: dict) -> dict | None:
        for d in self.docs:
            if _matches(d, query):
                before = copy.deepcopy(d)
                d.update(update["$set"])
                return before
        return None


class FakeRuns:
    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], dict] = {}

    async def find_one(self, q: dict) -> dict | None:
        return copy.deepcopy(self.docs.get((q["date"], q["channel_id"])))

    async def update_one(self, q: dict, update: dict, upsert: bool = False) -> None:
        doc = self.docs.setdefault((q["date"], q["channel_id"]), {"slots": {}})
        for dotted, val in update.get("$set", {}).items():
            cur = doc
            *path, last = dotted.split(".")
            for p in path:
                cur = cur.setdefault(p, {})
            cur[last] = val


class FakeDB:
    def __init__(self) -> None:
        self.source_callbacks = FakeCallbacks()
        self.auto_scheduler_runs = FakeRuns()


# ------------------------------------------------------------------
# The password
# ------------------------------------------------------------------


def test_bearer_token_parsing():
    assert cb.bearer_token("Bearer abc") == "abc"
    assert cb.bearer_token("bearer   abc  ") == "abc"
    assert cb.bearer_token("Basic abc") is None
    assert cb.bearer_token("Bearer ") is None
    assert cb.bearer_token(None) is None


def test_callback_expires_at_midnight_after_its_day():
    assert cb.callback_expiry(DAY) == datetime(2026, 8, 25, 0, 0, tzinfo=IST)


@pytest.mark.asyncio
async def test_only_a_hash_of_the_token_is_stored():
    db = FakeDB()
    offer = await cb.issue(db, SLOT, "s-geo", _dt(18))
    stored = db.source_callbacks.docs[0]
    assert offer.token not in str(stored)
    assert cb.token_matches(offer.token, stored["token_hash"])
    assert offer.url == f"https://us.example/api/v1/source-callbacks/{offer.callback_id}"
    assert offer.headers() == {"X-Callback-Url": offer.url, "X-Callback-Token": offer.token}


@pytest.mark.asyncio
async def test_a_callback_can_be_claimed_exactly_once():
    db = FakeDB()
    offer = await cb.issue(db, SLOT, "s-geo", _dt(18))
    assert (await cb.claim(db, offer.callback_id, offer.token, _dt(18, 30)))[0] == "claimed"
    assert (await cb.claim(db, offer.callback_id, offer.token, _dt(18, 31)))[0] == "already_received"


@pytest.mark.asyncio
async def test_a_wrong_token_is_refused_before_anything_else_is_revealed():
    db = FakeDB()
    offer = await cb.issue(db, SLOT, "s-geo", _dt(18))
    await cb.close(db, offer.callback_id, _dt(18, 5), "test")
    assert (await cb.claim(db, offer.callback_id, "guess", _dt(18, 30)))[0] == "unauthorized"
    assert (await cb.claim(db, offer.callback_id, None, _dt(18, 30)))[0] == "unauthorized"
    assert (await cb.claim(db, "nope", offer.token, _dt(18, 30)))[0] == "not_found"


@pytest.mark.asyncio
async def test_closed_and_overdue_callbacks_are_gone():
    db = FakeDB()
    closed = await cb.issue(db, SLOT, "s-geo", _dt(18))
    await cb.close(db, closed.callback_id, _dt(18, 5), "slot passed")
    assert (await cb.claim(db, closed.callback_id, closed.token, _dt(18, 30)))[0] == "gone"

    late = await cb.issue(db, SLOT, "s-geo", _dt(18))
    next_day = datetime(2026, 8, 25, 0, 1, tzinfo=IST)
    assert (await cb.claim(db, late.callback_id, late.token, next_day))[0] == "gone"


@pytest.mark.asyncio
async def test_the_next_day_sweep_expires_callbacks_that_never_came():
    db = FakeDB()
    await cb.issue(db, SLOT, "s-geo", _dt(18))
    assert await cb.expire_overdue(db, _dt(23, 59)) == 0
    assert await cb.expire_overdue(db, datetime(2026, 8, 25, 0, 5, tzinfo=IST)) == 1
    assert db.source_callbacks.docs[0]["status"] == "expired"


# ------------------------------------------------------------------
# The service: an offer survives only if the app took it
# ------------------------------------------------------------------


def _service(monkeypatch, answer: TodaysVideo | Exception):
    db = FakeDB()
    seen: dict = {}

    class _Adapter:
        def supports_todays_video(self, source):
            return True

        async def request_todays_video(self, source, callback_headers=None):
            seen["headers"] = callback_headers
            if isinstance(answer, Exception):
                raise answer
            return copy.copy(answer)

    monkeypatch.setattr(vss, "adapter_for", lambda source: _Adapter())
    svc = vss.VideoSourceService(db)

    async def _require(channel_id, source_id):
        return SimpleNamespace(name="GeoRank")

    async def _health(*a, **kw):
        return None

    monkeypatch.setattr(svc, "_require_source", _require)
    monkeypatch.setattr(svc, "_record_health", _health)
    return db, svc, seen


@pytest.mark.asyncio
async def test_an_accepted_offer_is_kept_and_named_on_the_result(monkeypatch):
    db, svc, seen = _service(monkeypatch, TodaysVideo(state="generating", callback_accepted=True))
    result = await svc.todays_video("histriphy", "s-geo", callback_slot=SLOT)
    record = db.source_callbacks.docs[0]
    assert result.callback_id == record["callback_id"]
    assert record["status"] == "pending"
    assert seen["headers"]["X-Callback-Url"].endswith(record["callback_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        TodaysVideo(state="generating", callback_accepted=False),  # an app that predates callbacks
        TodaysVideo(state="ready", video_id="v1"),
        TodaysVideo(state="unavailable", reason="busy"),
        RuntimeError("network down"),
    ],
)
async def test_an_offer_the_app_did_not_take_is_closed(monkeypatch, answer):
    db, svc, _ = _service(monkeypatch, answer)
    result = await svc.todays_video("histriphy", "s-geo", callback_slot=SLOT)
    assert result.callback_id is None
    assert db.source_callbacks.docs[0]["status"] == "closed"


@pytest.mark.asyncio
async def test_no_offer_is_made_without_a_slot(monkeypatch):
    db, svc, seen = _service(monkeypatch, TodaysVideo(state="generating", callback_accepted=True))
    await svc.todays_video("histriphy", "s-geo")
    assert seen["headers"] is None
    assert db.source_callbacks.docs == []


# ------------------------------------------------------------------
# The scheduler
# ------------------------------------------------------------------


class FakeService:
    def __init__(self, today: TodaysVideo, queued: bool = True) -> None:
        self._today = today
        self._queued = queued
        self.today_calls = 0
        self.enqueued: list[list[str]] = []

    async def todays_video(self, channel_id, source_id, callback_slot=None):
        self.today_calls += 1
        return self._today

    async def enqueue_import(self, channel_id, source_id, ids):
        self.enqueued.append(list(ids))
        if not self._queued:
            return {"queued": [], "skipped": [{"reason": "already imported"}]}
        return {"queued": [{"video_id": "ours-1"}]}


def _waiting_slot(callback_id: str | None) -> dict:
    return {
        "state": cron._GENERATING,
        "source": "GeoRank",
        "source_id": "s-geo",
        "via_today": True,
        "awaiting_since": _dt(18),
        "generation_requested_at": _dt(18),
        "last_polled_at": _dt(18),
        "retry_after_seconds": 180,
        "callback_id": callback_id,
    }


def _seed(db: FakeDB, data: dict) -> None:
    db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")] = {"slots": {"19:00": data}}


def _slot(db: FakeDB) -> dict:
    return db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]


CHANNEL = {"channel_id": "histriphy"}


@pytest.mark.asyncio
async def test_a_promised_callback_means_no_polling_before_the_deadline():
    db = FakeDB()
    _seed(db, _waiting_slot("cb-1"))
    service = FakeService(TodaysVideo(state="ready", video_id="would-be-seen"))

    await cron._poll_todays_video(db, CHANNEL, service, "19:00", _slot(db), DAY, _dt(18, 40))

    assert service.today_calls == 0
    assert _slot(db)["state"] == cron._GENERATING


@pytest.mark.asyncio
async def test_without_a_callback_the_slot_still_polls():
    db = FakeDB()
    _seed(db, _waiting_slot(None))
    service = FakeService(TodaysVideo(state="ready", video_id="v-1"))

    await cron._poll_todays_video(db, CHANNEL, service, "19:00", _slot(db), DAY, _dt(18, 40))

    assert service.today_calls == 1
    assert _slot(db)["state"] == cron._IMPORTING


@pytest.mark.asyncio
async def test_the_deadline_makes_one_last_ask_that_rescues_a_lost_callback():
    db = FakeDB()
    offer = await cb.issue(db, SLOT, "s-geo", _dt(18))
    _seed(db, _waiting_slot(offer.callback_id))
    service = FakeService(TodaysVideo(state="ready", video_id="v-done"))

    await cron._poll_todays_video(db, CHANNEL, service, "19:00", _slot(db), DAY, _dt(19, 3))

    assert service.today_calls == 1
    assert _slot(db)["state"] == cron._IMPORTING
    assert service.enqueued == [["v-done"]]
    assert db.source_callbacks.docs[0]["status"] == "closed"


@pytest.mark.asyncio
async def test_at_the_deadline_with_nothing_ready_the_slot_is_skipped_and_the_callback_closed():
    db = FakeDB()
    offer = await cb.issue(db, SLOT, "s-geo", _dt(18))
    _seed(db, _waiting_slot(offer.callback_id))
    service = FakeService(TodaysVideo(state="generating"))

    await cron._poll_todays_video(db, CHANNEL, service, "19:00", _slot(db), DAY, _dt(19, 3))

    assert _slot(db)["state"] == cron._SKIPPED
    assert db.source_callbacks.docs[0]["status"] == "closed"


def _record(callback_id: str = "cb-1") -> dict:
    return {
        "callback_id": callback_id,
        "channel_id": "histriphy",
        "source_id": "s-geo",
        "day": "2026-08-24",
        "slot": "19:00",
    }


@pytest.mark.asyncio
async def test_a_ready_callback_imports_the_named_video(monkeypatch):
    db = FakeDB()
    _seed(db, _waiting_slot("cb-1"))
    service = FakeService(TodaysVideo(state="generating"))
    monkeypatch.setattr(cron, "VideoSourceService", lambda _db: service)

    body = TodayCallbackBody.model_validate({"status": "ready", "video": {"id": "ec2-abc"}, "source": "format"})
    action = await cron.settle_today_callback(db, _record(), body, _dt(18, 20))

    assert action == "importing"
    assert service.enqueued == [["ec2-abc"]]
    assert _slot(db)["state"] == cron._IMPORTING
    assert _slot(db)["import_started_at"] == _dt(18, 20)


@pytest.mark.asyncio
async def test_a_failed_callback_closes_the_slot_with_the_apps_reason():
    db = FakeDB()
    _seed(db, _waiting_slot("cb-1"))
    body = TodayCallbackBody(status="failed", error="render timed out 9 times")

    action = await cron.settle_today_callback(db, _record(), body, _dt(18, 20))

    assert action == "failed_recorded"
    assert _slot(db)["state"] == cron._FAILED
    assert "render timed out 9 times" in _slot(db)["reason"]


@pytest.mark.asyncio
async def test_a_callback_for_a_slot_that_moved_on_is_refused():
    db = FakeDB()
    _seed(db, {**_waiting_slot("cb-1"), "state": cron._SKIPPED})
    body = TodayCallbackBody.model_validate({"status": "ready", "video": {"id": "late"}})
    assert await cron.settle_today_callback(db, _record(), body, _dt(19, 30)) == "slot_closed"

    _seed(db, _waiting_slot("a-newer-callback"))
    assert await cron.settle_today_callback(db, _record(), body, _dt(18, 30)) == "slot_closed"


@pytest.mark.asyncio
async def test_a_video_we_cannot_take_sends_the_slot_back_to_polling(monkeypatch):
    db = FakeDB()
    _seed(db, _waiting_slot("cb-1"))
    monkeypatch.setattr(
        cron, "VideoSourceService", lambda _db: FakeService(TodaysVideo(state="generating"), queued=False)
    )
    body = TodayCallbackBody.model_validate({"status": "ready", "video": {"id": "dup"}})

    assert await cron.settle_today_callback(db, _record(), body, _dt(18, 20)) == "polling"
    assert _slot(db)["callback_id"] is None
    assert _slot(db)["state"] == cron._GENERATING


def test_a_ready_body_must_name_its_video():
    with pytest.raises(ValueError):
        TodayCallbackBody.model_validate({"status": "ready"})


# ------------------------------------------------------------------
# The route
# ------------------------------------------------------------------


@pytest.fixture
def client_and_db(monkeypatch):
    db = FakeDB()
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_db] = lambda: db
    settled: list[str] = []

    async def fake_settle(_db, record, body, now):
        settled.append(body.status)
        return "importing"

    monkeypatch.setattr(route, "settle_today_callback", fake_settle)
    monkeypatch.setattr(route, "wake_auto_scheduler", lambda: settled.append("woken"))
    return TestClient(app), db, settled


def _post(client: TestClient, callback_id: str, token: str | None, body: dict):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(f"/api/v1/source-callbacks/{callback_id}", json=body, headers=headers)


READY = {"status": "ready", "video": {"id": "ec2-1"}}


def _today_slot() -> cb.CallbackSlot:
    # The route checks expiry against the real clock, so its records are for today.
    return cb.CallbackSlot(
        channel_id="histriphy", day=datetime.now(IST).date(), slot="19:00", public_base_url="https://us.example"
    )


@pytest.mark.asyncio
async def test_route_accepts_once_then_refuses(client_and_db):
    client, db, settled = client_and_db
    offer = await cb.issue(db, _today_slot(), "s-geo", datetime.now(IST) - timedelta(minutes=1))

    first = _post(client, offer.callback_id, offer.token, READY)
    assert first.status_code == 200
    assert first.json() == {"received": True, "action": "importing"}
    assert settled == ["ready", "woken"]
    assert db.source_callbacks.docs[0]["outcome"] == "importing"

    assert _post(client, offer.callback_id, offer.token, READY).status_code == 409


@pytest.mark.asyncio
async def test_route_refuses_bad_auth_unknown_ids_and_bad_bodies(client_and_db):
    client, db, settled = client_and_db
    offer = await cb.issue(db, _today_slot(), "s-geo", datetime.now(IST))

    assert _post(client, offer.callback_id, None, READY).status_code == 401
    assert _post(client, offer.callback_id, "wrong", READY).status_code == 401
    assert _post(client, "unknown", offer.token, READY).status_code == 404
    assert _post(client, offer.callback_id, offer.token, {"status": "ready"}).status_code == 422
    assert settled == []  # nothing reached the scheduler
    assert db.source_callbacks.docs[0]["status"] == "pending"  # a bad body does not burn the password


@pytest.mark.asyncio
async def test_route_says_gone_for_a_closed_callback(client_and_db):
    client, db, _ = client_and_db
    offer = await cb.issue(db, _today_slot(), "s-geo", datetime.now(IST))
    await cb.close(db, offer.callback_id, datetime.now(IST), "slot passed")
    assert _post(client, offer.callback_id, offer.token, READY).status_code == 410
