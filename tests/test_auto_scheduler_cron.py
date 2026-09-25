"""Auto-scheduler cron orchestration: slot filling, import recheck, and summary.

The pure decisions are covered in ``test_auto_scheduler_selection``. Here we drive
``process_channel`` and the summary latch against small in-memory fakes, patching
the side-effect boundaries (scheduling, import picking) so no DB or network runs.
"""

import copy
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.auto_scheduler_cron as cron
from app.timezone import IST


def _dt(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=IST)


# ------------------------------------------------------------------
# fakes
# ------------------------------------------------------------------


class FakeRuns:
    """Stand-in for db.auto_scheduler_runs with dotted-$set and $setOnInsert."""

    def __init__(self):
        self.docs: dict[tuple[str, str], dict] = {}

    async def find_one(self, q):
        return copy.deepcopy(self.docs.get((q["date"], q["channel_id"])))

    def find(self, q):
        """Answers the ``{"date": {"$in": [...]}}`` query the rollover sweep makes."""
        wanted = set(q.get("date", {}).get("$in", []))
        return _IdCursor([copy.deepcopy(d) for (day, _cid), d in self.docs.items() if day in wanted])

    async def update_one(self, q, update, upsert=False):
        key = (q["date"], q["channel_id"])
        if "$setOnInsert" in update and key not in self.docs:
            self.docs[key] = copy.deepcopy(update["$setOnInsert"])
        if "$set" in update:
            doc = self.docs.setdefault(key, {"date": q["date"], "channel_id": q["channel_id"], "slots": {}})
            for dotted, val in update["$set"].items():
                parts = dotted.split(".")
                cur = doc
                for p in parts[:-1]:
                    cur = cur.setdefault(p, {})
                cur[parts[-1]] = val


class _IdCursor:
    """Answers an ``{"field": {"$in": [...]}}`` find, which is all the summary needs."""

    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return list(self._docs)


class FakeVideos:
    def __init__(self, by_id=None):
        self.by_id = by_id or {}

    async def find_one(self, q):
        return self.by_id.get(q.get("video_id"))

    def find(self, q, _projection=None):
        wanted = q.get("video_id", {}).get("$in", [])
        return _IdCursor([{"video_id": vid, **self.by_id[vid]} for vid in wanted if vid in self.by_id])


class FakeChannels:
    def __init__(self, by_id=None):
        self.by_id = by_id or {}

    def find(self, q, _projection=None):
        wanted = q.get("channel_id", {}).get("$in", [])
        return _IdCursor([{"channel_id": cid, **self.by_id[cid]} for cid in wanted if cid in self.by_id])


class FakeSummaries:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    async def update_one(self, q, update, upsert=False):
        key = q["date"]
        if "$setOnInsert" in update and key not in self.docs:
            self.docs[key] = dict(update["$setOnInsert"])

    async def find_one_and_update(self, q, update, return_document=False):
        doc = self.docs.get(q["date"])
        if doc is not None and doc.get("sent") == q.get("sent"):
            pre = dict(doc)
            doc.update(update["$set"])
            return pre
        return None


class FakeSingleDoc:
    def __init__(self, doc=None):
        self.doc = doc

    async def find_one(self, q=None, proj=None):
        return self.doc


class FakeDB:
    def __init__(self, runs=None, videos=None, summaries=None, config_doc=None, profile=None, channels=None):
        self.auto_scheduler_runs = runs or FakeRuns()
        self.videos = videos or FakeVideos()
        self.channels = channels or FakeChannels()
        self.auto_scheduler_summaries = summaries or FakeSummaries()
        self.config = FakeSingleDoc(config_doc)
        self.profiles = FakeSingleDoc(profile)


def _channel(times, name="Histriphy", cid="histriphy"):
    return {
        "channel_id": cid,
        "name": name,
        "platform": "youtube",
        "automation_config": {"auto_scheduler": {"enabled": True, "schedule_times": times}},
    }


TIMING = cron._Timing(recheck_minutes=35, max_wait_minutes=90)


# ------------------------------------------------------------------
# Phase A — schedule from Ready
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_due_slot_schedules_oldest_ready_video(monkeypatch):
    db = FakeDB()
    scheduled_calls = []

    async def fake_committed(db_, cid, day):
        return []

    async def fake_ready(db_, cid):
        return [
            {"video_id": "old", "status": "ready", "created_at": "2026-08-18T00:00:00+05:30"},
            {"video_id": "new", "status": "ready", "created_at": "2026-08-22T00:00:00+05:30"},
        ]

    async def fake_schedule(db_, channel, video_doc, schedule_at, now_=None):
        scheduled_calls.append((video_doc["video_id"], schedule_at))
        return {"status": "queued"}

    monkeypatch.setattr(cron, "_channel_videos_today", fake_committed)
    monkeypatch.setattr(cron, "_ready_videos", fake_ready)
    monkeypatch.setattr(cron, "_schedule_video", fake_schedule)

    now = _dt(2026, 8, 24, 18, 30)
    await cron.process_channel(db, _channel(["19:00"]), service=None, day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._SCHEDULED
    assert slot["video_id"] == "old"  # FIFO oldest
    assert scheduled_calls == [("old", _dt(2026, 8, 24, 19, 0))]


# ------------------------------------------------------------------
# Phase A — Ready empty → trigger import
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_ready_triggers_import_and_marks_slot_importing(monkeypatch):
    db = FakeDB()
    enqueue_calls = []

    async def fake_committed(db_, cid, day):
        return []

    async def fake_ready(db_, cid):
        return []

    async def fake_pick(service, cid):
        return ("src1", "GeoRank", "sv-1")

    class Service:
        async def enqueue_import(self, cid, source_id, ids):
            enqueue_calls.append((cid, source_id, ids))
            return {"queued": [{"video_id": "imp-1", "source_video_id": "sv-1"}], "skipped": []}

    monkeypatch.setattr(cron, "_channel_videos_today", fake_committed)
    monkeypatch.setattr(cron, "_ready_videos", fake_ready)
    monkeypatch.setattr(cron, "_pick_import_across_sources", fake_pick)

    now = _dt(2026, 8, 24, 18, 30)
    await cron.process_channel(db, _channel(["19:00"]), service=Service(), day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._IMPORTING
    assert slot["video_id"] == "imp-1"
    assert slot["source"] == "GeoRank"
    assert slot["awaiting_since"] == now
    assert enqueue_calls == [("histriphy", "src1", ["sv-1"])]


@pytest.mark.asyncio
async def test_empty_ready_and_no_import_skips_the_slot(monkeypatch):
    db = FakeDB()

    async def fake_committed(db_, cid, day):
        return []

    async def fake_ready(db_, cid):
        return []

    async def fake_pick(service, cid):
        return None

    monkeypatch.setattr(cron, "_channel_videos_today", fake_committed)
    monkeypatch.setattr(cron, "_ready_videos", fake_ready)
    monkeypatch.setattr(cron, "_pick_import_across_sources", fake_pick)

    now = _dt(2026, 8, 24, 18, 30)
    # No source can render, so there is genuinely nothing left to try.
    await cron.process_channel(
        db, _channel(["19:00"]), service=FakeService(sources=[]), day=now.date(), now=now, timing=TIMING
    )

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._SKIPPED
    assert "no source can generate" in slot["reason"]


@pytest.mark.asyncio
async def test_frequency_met_does_nothing(monkeypatch):
    db = FakeDB()

    async def fake_committed(db_, cid, day):
        # One video already scheduled today satisfies the single slot.
        return [{"status": "scheduled", "scheduled_at": _dt(2026, 8, 24, 19, 0)}]

    async def fake_ready(db_, cid):
        raise AssertionError("should not look at Ready when pace is already met")

    monkeypatch.setattr(cron, "_channel_videos_today", fake_committed)
    monkeypatch.setattr(cron, "_ready_videos", fake_ready)

    now = _dt(2026, 8, 24, 18, 30)
    await cron.process_channel(db, _channel(["19:00"]), service=None, day=now.date(), now=now, timing=TIMING)

    slots = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]
    assert slots == {}  # nothing acted on


# ------------------------------------------------------------------
# Phase B — recheck an in-flight import
# ------------------------------------------------------------------


def _seed_importing(db, awaiting_since, video_id="imp-1"):
    db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")] = {
        "date": "2026-08-24",
        "channel_id": "histriphy",
        "channel_name": "Histriphy",
        "slots": {
            "19:00": {
                "state": cron._IMPORTING,
                "video_id": video_id,
                "source": "GeoRank",
                "awaiting_since": awaiting_since,
            }
        },
    }


@pytest.mark.asyncio
async def test_recheck_schedules_once_import_is_ready(monkeypatch):
    db = FakeDB(videos=FakeVideos({"imp-1": {"video_id": "imp-1", "status": "ready", "channel_id": "histriphy"}}))
    _seed_importing(db, awaiting_since=_dt(2026, 8, 24, 18, 0))
    scheduled = []

    async def fake_committed(db_, cid, day):
        return []

    async def fake_ready(db_, cid):
        return []

    async def fake_schedule(db_, channel, video_doc, schedule_at, now_=None):
        scheduled.append(video_doc["video_id"])
        return {"status": "queued"}

    monkeypatch.setattr(cron, "_channel_videos_today", fake_committed)
    monkeypatch.setattr(cron, "_ready_videos", fake_ready)
    monkeypatch.setattr(cron, "_schedule_video", fake_schedule)

    now = _dt(2026, 8, 24, 18, 40)  # 40 min after the import — past the recheck window
    await cron.process_channel(db, _channel(["19:00"]), service=None, day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._SCHEDULED
    assert scheduled == ["imp-1"]


@pytest.mark.asyncio
async def test_recheck_leaves_slot_untouched_before_the_window(monkeypatch):
    db = FakeDB(videos=FakeVideos({"imp-1": {"video_id": "imp-1", "status": "processing", "channel_id": "histriphy"}}))
    _seed_importing(db, awaiting_since=_dt(2026, 8, 24, 18, 0))

    async def fake_committed(db_, cid, day):
        return []

    async def fake_ready(db_, cid):
        return []

    monkeypatch.setattr(cron, "_channel_videos_today", fake_committed)
    monkeypatch.setattr(cron, "_ready_videos", fake_ready)

    now = _dt(2026, 8, 24, 18, 10)  # only 10 min — too soon to recheck
    await cron.process_channel(db, _channel(["19:00"]), service=None, day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._IMPORTING  # still waiting


@pytest.mark.asyncio
async def test_recheck_fails_the_slot_after_max_wait(monkeypatch):
    db = FakeDB(videos=FakeVideos({"imp-1": {"video_id": "imp-1", "status": "analyzing", "channel_id": "histriphy"}}))
    _seed_importing(db, awaiting_since=_dt(2026, 8, 24, 18, 0))

    async def fake_committed(db_, cid, day):
        return []

    async def fake_ready(db_, cid):
        return []

    monkeypatch.setattr(cron, "_channel_videos_today", fake_committed)
    monkeypatch.setattr(cron, "_ready_videos", fake_ready)

    now = _dt(2026, 8, 24, 19, 45)  # 105 min later — past the 90 min max wait
    await cron.process_channel(db, _channel(["19:00"]), service=None, day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._FAILED


# ------------------------------------------------------------------
# End-of-day summary
# ------------------------------------------------------------------


def test_all_slots_terminal_is_false_before_slot_time():
    ch = _channel(["19:00"])
    run_docs = {"histriphy": {"slots": {"19:00": {"state": cron._SCHEDULED}}}}
    assert cron._all_slots_terminal([ch], run_docs, _dt(2026, 8, 24, 18, 0), date(2026, 8, 24)) is False


def test_all_slots_terminal_true_when_every_slot_done_and_past():
    ch = _channel(["19:00", "21:00"])
    run_docs = {"histriphy": {"slots": {"19:00": {"state": cron._SCHEDULED}, "21:00": {"state": cron._SKIPPED}}}}
    assert cron._all_slots_terminal([ch], run_docs, _dt(2026, 8, 24, 21, 30), date(2026, 8, 24)) is True


def test_all_slots_terminal_false_while_an_import_is_pending():
    ch = _channel(["19:00"])
    run_docs = {"histriphy": {"slots": {"19:00": {"state": cron._IMPORTING}}}}
    assert cron._all_slots_terminal([ch], run_docs, _dt(2026, 8, 24, 21, 30), date(2026, 8, 24)) is False


def test_assemble_summary_splits_scheduled_and_skipped():
    run_docs = {
        "histriphy": {
            "channel_id": "histriphy",
            "channel_name": "Histriphy",
            "slots": {
                "19:00": {"state": cron._SCHEDULED, "video_id": "v1", "source": "GeoRank"},
                "21:00": {"state": cron._SKIPPED, "reason": "no videos available to import"},
            },
        }
    }
    summary = cron._assemble_summary(cron.date(2026, 8, 24), run_docs)
    assert summary["date"] == "2026-08-24"
    assert len(summary["scheduled"]) == 1
    assert summary["scheduled"][0]["video_id"] == "v1"
    assert len(summary["skipped"]) == 1
    assert summary["skipped"][0]["reason"] == "no videos available to import"


@pytest.mark.asyncio
async def test_summary_is_sent_exactly_once(monkeypatch):
    runs = FakeRuns()
    runs.docs[("2026-08-24", "histriphy")] = {
        "date": "2026-08-24",
        "channel_id": "histriphy",
        "channel_name": "Histriphy",
        "slots": {"19:00": {"state": cron._SCHEDULED, "video_id": "v1"}},
    }
    db = FakeDB(runs=runs, profile={"email": "owner@example.com"})
    sends = []

    async def fake_send(settings, recipient, subject, body, html_body=None):
        sends.append(recipient)
        return True

    monkeypatch.setattr(cron, "send_email", fake_send)

    settings = SimpleNamespace(SUMMARY_EMAIL_TO=None)
    channels = [_channel(["19:00"])]
    now = _dt(2026, 8, 24, 19, 30)

    await cron._maybe_send_summary(db, settings, channels, now.date(), now)
    await cron._maybe_send_summary(db, settings, channels, now.date(), now)

    assert sends == ["owner@example.com"]  # sent once, to the profile fallback


@pytest.mark.asyncio
async def test_summary_prefers_configured_recipient(monkeypatch):
    runs = FakeRuns()
    runs.docs[("2026-08-24", "histriphy")] = {
        "date": "2026-08-24",
        "channel_id": "histriphy",
        "channel_name": "Histriphy",
        "slots": {"19:00": {"state": cron._SKIPPED, "reason": "no videos available to import"}},
    }
    db = FakeDB(runs=runs, profile={"email": "owner@example.com"})
    sends = []

    async def fake_send(settings, recipient, subject, body, html_body=None):
        sends.append(recipient)
        return True

    monkeypatch.setattr(cron, "send_email", fake_send)

    settings = SimpleNamespace(SUMMARY_EMAIL_TO="ops@example.com")
    now = _dt(2026, 8, 24, 19, 30)
    await cron._maybe_send_summary(db, settings, [_channel(["19:00"])], now.date(), now)

    assert sends == ["ops@example.com"]


# ------------------------------------------------------------------
# Phase A/C — asking an app to render, then importing what it made
# ------------------------------------------------------------------


def _source(source_id="s-geo", name="GeoRank", eta=15, per_day=4, enabled=True, can_generate=True):
    return SimpleNamespace(
        source_id=source_id,
        name=name,
        enabled=enabled,
        supports_generation=can_generate,
        generation_eta_minutes=eta if can_generate else None,
        generation_max_per_day=per_day,
    )


class FakeService:
    """Stands in for VideoSourceService across the generation calls."""

    def __init__(self, sources=None, already_today=0, state="pending", request_ok=True):
        self._sources = sources if sources is not None else [_source()]
        self._already_today = already_today
        self._state = state
        self._request_ok = request_ok
        self.requested: list[str] = []
        self.enqueued: list[tuple[str, list[str]]] = []

    async def list_sources(self, _channel_id):
        return list(self._sources)

    async def generations_today(self, _channel_id, _source_id, _since):
        return self._already_today

    async def request_generation(self, _channel_id, source_id):
        self.requested.append(source_id)
        if not self._request_ok:
            return {"ok": False, "error": "HTTP 503 — app is down"}
        return {"ok": True, "job_id": f"job-{len(self.requested)}", "source_name": "GeoRank"}

    async def generation_state(self, _channel_id, _source_id, _job_id):
        return self._state

    async def enqueue_import(self, _channel_id, source_id, ids):
        self.enqueued.append((source_id, list(ids)))
        return {"queued": [{"video_id": "rendered-1"}]}


def _no_ready(monkeypatch, pick=None):
    """Nothing committed, nothing in Ready, and a configurable import pick."""

    async def fake_committed(db_, cid, day):
        return []

    async def fake_ready(db_, cid):
        return []

    async def fake_pick(service, cid):
        return pick

    monkeypatch.setattr(cron, "_channel_videos_today", fake_committed)
    monkeypatch.setattr(cron, "_ready_videos", fake_ready)
    monkeypatch.setattr(cron, "_pick_import_across_sources", fake_pick)


def _seed_generating(db, awaiting_since, job_id="job-1", slot="19:00"):
    db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")] = {
        "date": "2026-08-24",
        "channel_id": "histriphy",
        "channel_name": "Histriphy",
        "slots": {
            slot: {
                "state": cron._GENERATING,
                "source": "GeoRank",
                "source_id": "s-geo",
                "job_id": job_id,
                "awaiting_since": awaiting_since,
            }
        },
    }


@pytest.mark.asyncio
async def test_nothing_to_import_asks_a_capable_source_to_render(monkeypatch):
    db = FakeDB()
    _no_ready(monkeypatch)
    service = FakeService()

    now = _dt(2026, 8, 24, 18, 0)  # an hour before the slot
    await cron.process_channel(db, _channel(["19:00"]), service=service, day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._GENERATING
    assert slot["job_id"] == "job-1"
    assert service.requested == ["s-geo"]


@pytest.mark.asyncio
async def test_a_source_too_slow_for_the_slot_is_never_asked(monkeypatch):
    """The whole point of the ETA: do not commission work that cannot arrive."""
    db = FakeDB()
    _no_ready(monkeypatch)
    service = FakeService(sources=[_source(eta=30)])

    now = _dt(2026, 8, 24, 18, 40)  # only 20 minutes of headroom
    await cron.process_channel(db, _channel(["19:00"]), service=service, day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._SKIPPED
    assert "in time" in slot["reason"]
    assert service.requested == []


@pytest.mark.asyncio
async def test_daily_cap_stops_a_stuck_catalogue_spending_forever(monkeypatch):
    db = FakeDB()
    _no_ready(monkeypatch)
    service = FakeService(sources=[_source(per_day=4)], already_today=4)

    now = _dt(2026, 8, 24, 18, 0)
    await cron.process_channel(db, _channel(["19:00"]), service=service, day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._SKIPPED
    assert "limit reached" in slot["reason"]
    assert service.requested == []


@pytest.mark.asyncio
async def test_a_refused_request_ends_the_slot_rather_than_retrying(monkeypatch):
    db = FakeDB()
    _no_ready(monkeypatch)
    service = FakeService(request_ok=False)

    now = _dt(2026, 8, 24, 18, 0)
    await cron.process_channel(db, _channel(["19:00"]), service=service, day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._SKIPPED
    assert "app is down" in slot["reason"]


@pytest.mark.asyncio
async def test_one_pass_with_many_empty_slots_does_not_fire_a_render_each(monkeypatch):
    """_set_slot writes to the DB, not our in-memory run doc — the cap must still hold."""
    db = FakeDB()
    _no_ready(monkeypatch)
    service = FakeService()

    now = _dt(2026, 8, 24, 18, 0)
    channel = _channel(["18:30", "18:45", "19:00"])
    await cron.process_channel(db, channel, service=service, day=now.date(), now=now, timing=TIMING)

    assert len(service.requested) == cron._MAX_CONCURRENT_GENERATIONS


@pytest.mark.asyncio
async def test_a_render_that_lands_is_imported_for_its_slot(monkeypatch):
    db = FakeDB()
    _no_ready(monkeypatch, pick=("s-geo", "GeoRank", "src-vid-1"))
    _seed_generating(db, awaiting_since=_dt(2026, 8, 24, 18, 0))
    service = FakeService(state="completed")

    now = _dt(2026, 8, 24, 18, 20)
    await cron.process_channel(db, _channel(["19:00"]), service=service, day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._IMPORTING
    assert slot["video_id"] == "rendered-1"
    # Flagged so the import is watched on the tick, not the 35-minute recheck.
    assert slot["from_generation"] is True
    assert service.enqueued == [("s-geo", ["src-vid-1"])]


@pytest.mark.asyncio
async def test_a_failed_render_gives_up_without_waiting_out_the_slot(monkeypatch):
    db = FakeDB()
    _no_ready(monkeypatch)
    _seed_generating(db, awaiting_since=_dt(2026, 8, 24, 18, 0))
    service = FakeService(state="failed")

    now = _dt(2026, 8, 24, 18, 20)  # well before the slot
    await cron.process_channel(db, _channel(["19:00"]), service=service, day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._SKIPPED
    assert "render failed" in slot["reason"]


@pytest.mark.asyncio
async def test_a_render_still_missing_at_the_slot_is_skipped(monkeypatch):
    db = FakeDB()
    _no_ready(monkeypatch)
    _seed_generating(db, awaiting_since=_dt(2026, 8, 24, 18, 0))
    service = FakeService(state="pending")

    now = _dt(2026, 8, 24, 19, 0)  # the slot has arrived
    await cron.process_channel(db, _channel(["19:00"]), service=service, day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._SKIPPED
    assert "did not arrive" in slot["reason"]


@pytest.mark.asyncio
async def test_a_render_still_pending_before_the_slot_keeps_waiting(monkeypatch):
    db = FakeDB()
    _no_ready(monkeypatch)
    _seed_generating(db, awaiting_since=_dt(2026, 8, 24, 18, 0))
    service = FakeService(state="pending")

    now = _dt(2026, 8, 24, 18, 30)
    await cron.process_channel(db, _channel(["19:00"]), service=service, day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._GENERATING  # still cooking, left alone


@pytest.mark.asyncio
async def test_an_import_from_a_render_is_rechecked_on_the_tick(monkeypatch):
    """It has already spent most of its hour rendering; it cannot wait 35 minutes."""
    db = FakeDB(
        videos=FakeVideos({"rendered-1": {"video_id": "rendered-1", "status": "ready", "channel_id": "histriphy"}})
    )
    db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")] = {
        "date": "2026-08-24",
        "channel_id": "histriphy",
        "channel_name": "Histriphy",
        "slots": {
            "19:00": {
                "state": cron._IMPORTING,
                "video_id": "rendered-1",
                "source": "GeoRank",
                "awaiting_since": _dt(2026, 8, 24, 18, 40),
                "from_generation": True,
            }
        },
    }
    scheduled = []

    async def fake_schedule(db_, channel, video, when, now_=None):
        scheduled.append(video["video_id"])
        return {"status": "queued"}

    monkeypatch.setattr(cron, "_schedule_video", fake_schedule)
    _no_ready(monkeypatch)

    now = _dt(2026, 8, 24, 18, 42)  # two minutes later — far inside the 35
    await cron.process_channel(db, _channel(["19:00"]), service=FakeService(), day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._SCHEDULED
    assert scheduled == ["rendered-1"]


# ------------------------------------------------------------------
# Rollover recovery
# ------------------------------------------------------------------
#
# Every recheck phase works on *today's* run doc. A slot still ``importing`` when
# midnight passed was therefore never looked at again: it stayed non-terminal
# forever, and since the summary waits for every slot to be terminal, that day's
# email never sent either. One real day was lost exactly this way.


def _stale_runs(state, day="2026-08-24", extra=None):
    runs = FakeRuns()
    runs.docs[(day, "histriphy")] = {
        "date": day,
        "channel_id": "histriphy",
        "channel_name": "Histriphy",
        "slots": {"19:00": {"state": state, **(extra or {})}},
    }
    return runs


def _catch_sends(monkeypatch, sends):
    async def fake_send(settings, recipient, subject, body, html_body=None):
        sends.append((recipient, subject))
        return True

    monkeypatch.setattr(cron, "send_email", fake_send)


@pytest.mark.asyncio
async def test_a_slot_stranded_importing_overnight_is_resolved_and_reported(monkeypatch):
    runs = _stale_runs(cron._IMPORTING, extra={"video_id": "v1", "awaiting_since": datetime(2026, 8, 24, 13, 30)})
    db = FakeDB(runs=runs, profile={"email": "owner@example.com"})
    sends: list = []
    _catch_sends(monkeypatch, sends)

    now = _dt(2026, 8, 25, 0, 30)
    await cron._resolve_stale_days(db, SimpleNamespace(SUMMARY_EMAIL_TO=None), [_channel(["19:00"])], now.date(), now)

    slot = runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._FAILED
    assert slot["reason"] == "import not ready before the day ended"
    assert len(sends) == 1  # the summary that was held hostage finally goes out


@pytest.mark.asyncio
async def test_each_stranded_state_gets_its_own_resolution(monkeypatch):
    sends: list = []
    for state, expected_state, reason in [
        (cron._IMPORTING, cron._FAILED, "import not ready before the day ended"),
        (cron._GENERATING, cron._FAILED, "render not ready before the day ended"),
        (cron._PENDING, cron._SKIPPED, "slot passed with no video chosen"),
        ("something-new", cron._FAILED, "left unresolved when the day ended"),
    ]:
        runs = _stale_runs(state)
        db = FakeDB(runs=runs, profile={"email": "owner@example.com"})
        _catch_sends(monkeypatch, sends)
        now = _dt(2026, 8, 25, 0, 30)
        await cron._resolve_stale_days(
            db, SimpleNamespace(SUMMARY_EMAIL_TO=None), [_channel(["19:00"])], now.date(), now
        )
        slot = runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
        assert (slot["state"], slot["reason"]) == (expected_state, reason), state


@pytest.mark.asyncio
async def test_an_awaiting_linked_copy_is_closed_when_the_day_ends(monkeypatch):
    runs = _stale_runs(
        cron._SCHEDULED,
        extra={"video_id": "v1", "linked": [{"channel_id": "geo_ig", "state": cron._AWAITING, "video_id": "sib"}]},
    )
    db = FakeDB(runs=runs, profile={"email": "owner@example.com"})
    sends: list = []
    _catch_sends(monkeypatch, sends)

    now = _dt(2026, 8, 25, 0, 30)
    await cron._resolve_stale_days(db, SimpleNamespace(SUMMARY_EMAIL_TO=None), [_channel(["19:00"])], now.date(), now)

    link = runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]["linked"][0]
    assert link["state"] == cron._SKIPPED
    assert link["reason"] == "day ended before the linked copy was ready"
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_a_configured_slot_with_no_record_does_not_block_a_past_days_summary(monkeypatch):
    """A second slot added to the config leaves an unseen slot on old days, which
    reads as ``pending`` and would silence those summaries forever."""
    runs = _stale_runs(cron._SCHEDULED, extra={"video_id": "v1"})
    db = FakeDB(runs=runs, profile={"email": "owner@example.com"})
    sends: list = []
    _catch_sends(monkeypatch, sends)

    now = _dt(2026, 8, 25, 0, 30)
    await cron._resolve_stale_days(
        db, SimpleNamespace(SUMMARY_EMAIL_TO=None), [_channel(["19:00", "21:00"])], now.date(), now
    )

    slots = runs.docs[("2026-08-24", "histriphy")]["slots"]
    assert slots["21:00"] == {"state": cron._SKIPPED, "reason": "no attempt recorded"}
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_today_is_never_swept(monkeypatch):
    """Today's in-flight work is the scheduler's job, not the sweep's — resolving it
    would abandon an import that is minutes from landing."""
    runs = _stale_runs(cron._IMPORTING, day="2026-08-25")
    db = FakeDB(runs=runs, profile={"email": "owner@example.com"})
    sends: list = []
    _catch_sends(monkeypatch, sends)

    now = _dt(2026, 8, 25, 19, 30)
    await cron._resolve_stale_days(db, SimpleNamespace(SUMMARY_EMAIL_TO=None), [_channel(["19:00"])], now.date(), now)

    assert runs.docs[("2026-08-25", "histriphy")]["slots"]["19:00"]["state"] == cron._IMPORTING
    assert sends == []


@pytest.mark.asyncio
async def test_the_sweep_does_not_re_send_a_summary_already_reported(monkeypatch):
    """The sweep offers every day in its window to the summary; the per-date latch
    is what stops a restart emailing a week of history."""
    runs = _stale_runs(cron._SCHEDULED, extra={"video_id": "v1"})
    summaries = FakeSummaries()
    summaries.docs["2026-08-24"] = {"date": "2026-08-24", "sent": True}
    db = FakeDB(runs=runs, summaries=summaries, profile={"email": "owner@example.com"})
    sends: list = []
    _catch_sends(monkeypatch, sends)

    now = _dt(2026, 8, 25, 0, 30)
    await cron._resolve_stale_days(db, SimpleNamespace(SUMMARY_EMAIL_TO=None), [_channel(["19:00"])], now.date(), now)

    assert sends == []


@pytest.mark.asyncio
async def test_the_sweep_only_reaches_back_a_bounded_window(monkeypatch):
    runs = _stale_runs(cron._IMPORTING, day="2026-08-01")
    db = FakeDB(runs=runs, profile={"email": "owner@example.com"})
    sends: list = []
    _catch_sends(monkeypatch, sends)

    now = _dt(2026, 8, 25, 0, 30)  # 24 days on, well outside the 7-day window
    await cron._resolve_stale_days(db, SimpleNamespace(SUMMARY_EMAIL_TO=None), [_channel(["19:00"])], now.date(), now)

    assert runs.docs[("2026-08-01", "histriphy")]["slots"]["19:00"]["state"] == cron._IMPORTING
    assert sends == []
