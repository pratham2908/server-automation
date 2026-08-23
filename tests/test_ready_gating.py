"""Promotion from 'processing' to 'ready' once AI packaging is complete.

Every upload path now creates a video as 'processing'; it becomes postable only
after packaging succeeds. These tests pin the guards (never rewind a live video,
never promote before packaging) and the two outcomes: ready + posting queue, or
an Instagram upload-time schedule routed to the scheduler.
"""

from datetime import datetime

import pytest

import app.services.retention_analysis as ra
from app.services.retention_analysis import promote_processing_to_ready
from app.timezone import IST


class FakeColl:
    def __init__(self, docs=None):
        self.docs = docs or []

    @staticmethod
    def _match(doc, query):
        return all(doc.get(k) == v for k, v in query.items())

    async def find_one(self, query, sort=None):
        matches = [d for d in self.docs if self._match(d, query)]
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda d: d.get(key, 0), reverse=(direction == -1))
        return matches[0] if matches else None

    async def update_one(self, query, update):
        for d in self.docs:
            if self._match(d, query):
                d.update(update["$set"])
                return

    async def insert_one(self, doc):
        self.docs.append(dict(doc))


class FakeDB:
    def __init__(self, video, channel):
        self.videos = FakeColl([video] if video else [])
        self.posting_queue = FakeColl([])
        self.channels = FakeColl([channel] if channel else [])


def _video(**over):
    doc = {"channel_id": "c1", "video_id": "v1", "status": "processing", "packaging_status": "completed"}
    doc.update(over)
    return doc


def _yt_channel():
    return {"channel_id": "c1", "platform": "youtube"}


# ------------------------------------------------------------------
# happy path — ready + posting queue
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_packaged_processing_video_becomes_ready_and_is_queued():
    db = FakeDB(_video(), _yt_channel())
    await promote_processing_to_ready(db, "c1", "v1")
    assert db.videos.docs[0]["status"] == "ready"
    assert len(db.posting_queue.docs) == 1
    assert db.posting_queue.docs[0]["video_id"] == "v1"
    assert db.posting_queue.docs[0]["position"] == 1


# ------------------------------------------------------------------
# guards
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_promoted_until_packaging_completes():
    for pkg in ("pending", "analyzing", "failed"):
        db = FakeDB(_video(packaging_status=pkg), _yt_channel())
        await promote_processing_to_ready(db, "c1", "v1")
        assert db.videos.docs[0]["status"] == "processing"
        assert db.posting_queue.docs == []


@pytest.mark.asyncio
async def test_live_video_is_never_rewound():
    # A manual re-analysis of an already live/scheduled video must not touch it.
    for live in ("ready", "queued", "scheduled", "published"):
        db = FakeDB(_video(status=live), _yt_channel())
        await promote_processing_to_ready(db, "c1", "v1")
        assert db.videos.docs[0]["status"] == live
        assert db.posting_queue.docs == []


# ------------------------------------------------------------------
# upload-time schedule
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_instagram_upload_time_schedule_is_routed_to_the_scheduler(monkeypatch):
    calls = []

    async def fake_schedule(*, db, channel_id, video_doc, scheduled_at):
        calls.append((channel_id, video_doc["video_id"], scheduled_at))

    monkeypatch.setattr(ra, "schedule_single_video_instagram", fake_schedule)

    future = datetime(2099, 1, 1, 19, 0, tzinfo=IST)
    db = FakeDB(_video(scheduled_at=future), {"channel_id": "c1", "platform": "instagram"})
    await promote_processing_to_ready(db, "c1", "v1")

    assert calls == [("c1", "v1", future)]
    # Routed to the scheduler, not the ready/posting-queue path.
    assert db.videos.docs[0]["status"] == "processing"  # scheduler stub didn't flip it
    assert db.posting_queue.docs == []


@pytest.mark.asyncio
async def test_youtube_schedule_falls_through_to_ready():
    # YouTube upload-time schedules were never honoured at create; preserve that —
    # the video just becomes ready and is scheduled later via the scheduler.
    future = datetime(2099, 1, 1, 19, 0, tzinfo=IST)
    db = FakeDB(_video(scheduled_at=future), _yt_channel())
    await promote_processing_to_ready(db, "c1", "v1")
    assert db.videos.docs[0]["status"] == "ready"
    assert len(db.posting_queue.docs) == 1


@pytest.mark.asyncio
async def test_past_schedule_is_treated_as_ready_now():
    past = datetime(2000, 1, 1, 19, 0, tzinfo=IST)
    db = FakeDB(_video(scheduled_at=past), {"channel_id": "c1", "platform": "instagram"})
    await promote_processing_to_ready(db, "c1", "v1")
    assert db.videos.docs[0]["status"] == "ready"
    assert len(db.posting_queue.docs) == 1
