"""Auto-scheduler cron orchestration: slot filling, import recheck, and summary.

The pure decisions are covered in ``test_auto_scheduler_selection``. Here we drive
``process_channel`` and the summary latch against small in-memory fakes, patching
the side-effect boundaries (scheduling, import picking) so no DB or network runs.
"""

import copy
from datetime import datetime, timedelta
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


class FakeVideos:
    def __init__(self, by_id=None):
        self.by_id = by_id or {}

    async def find_one(self, q):
        return self.by_id.get(q.get("video_id"))


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
    def __init__(self, runs=None, videos=None, summaries=None, config_doc=None, profile=None):
        self.auto_scheduler_runs = runs or FakeRuns()
        self.videos = videos or FakeVideos()
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

    async def fake_schedule(db_, channel, video_doc, schedule_at):
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
    await cron.process_channel(db, _channel(["19:00"]), service=None, day=now.date(), now=now, timing=TIMING)

    slot = db.auto_scheduler_runs.docs[("2026-08-24", "histriphy")]["slots"]["19:00"]
    assert slot["state"] == cron._SKIPPED


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

    async def fake_schedule(db_, channel, video_doc, schedule_at):
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
    assert cron._all_slots_terminal([ch], run_docs, _dt(2026, 8, 24, 18, 0)) is False


def test_all_slots_terminal_true_when_every_slot_done_and_past():
    ch = _channel(["19:00", "21:00"])
    run_docs = {"histriphy": {"slots": {"19:00": {"state": cron._SCHEDULED}, "21:00": {"state": cron._SKIPPED}}}}
    assert cron._all_slots_terminal([ch], run_docs, _dt(2026, 8, 24, 21, 30)) is True


def test_all_slots_terminal_false_while_an_import_is_pending():
    ch = _channel(["19:00"])
    run_docs = {"histriphy": {"slots": {"19:00": {"state": cron._IMPORTING}}}}
    assert cron._all_slots_terminal([ch], run_docs, _dt(2026, 8, 24, 21, 30)) is False


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

    async def fake_send(settings, recipient, subject, body):
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

    async def fake_send(settings, recipient, subject, body):
        sends.append(recipient)
        return True

    monkeypatch.setattr(cron, "send_email", fake_send)

    settings = SimpleNamespace(SUMMARY_EMAIL_TO="ops@example.com")
    now = _dt(2026, 8, 24, 19, 30)
    await cron._maybe_send_summary(db, settings, [_channel(["19:00"])], now.date(), now)

    assert sends == ["ops@example.com"]
