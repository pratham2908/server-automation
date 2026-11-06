"""An upload-time schedule keeps its instant (6 Nov 2026: Mehr refs 117 and 122 were scheduled 5h30m early).

Two ways it went wrong:
* promotion read ``scheduled_at`` back from MongoDB — naive and in UTC — and relabelled it as IST;
* the create paths relabelled a string's own offset (``…Z``, ``…+00:00``) as IST instead of converting it.
These pin both, with a fake collection that stores datetimes the way MongoDB does (naive UTC).
"""

from __future__ import annotations

from datetime import datetime

import pytest

import app.services.retention_analysis as ra
from app.timezone import IST, UTC, parse_ist

WANT = datetime(2099, 1, 1, 19, 0, tzinfo=IST)  # 19:00 IST = 13:30 UTC


@pytest.mark.parametrize(
    "value", ["2099-01-01T19:00:00", "2099-01-01T19:00:00+05:30", "2099-01-01T13:30:00Z", "2099-01-01T13:30:00+00:00"]
)
def test_parse_ist_keeps_the_instant(value):
    got = parse_ist(value)
    assert got == WANT
    assert got.utcoffset() == IST.utcoffset(None)


def _bson(value):
    """What MongoDB gives back for a stored datetime: naive, UTC."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


class MongoLikeCollection:
    def __init__(self, docs=None):
        self.docs = [{k: _bson(v) for k, v in d.items()} for d in docs or []]

    async def find_one(self, query, _projection=None, sort=None):
        matches = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda d: d.get(key, 0), reverse=direction < 0)
        return dict(matches[0]) if matches else None

    async def update_one(self, query, update):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update({k: _bson(v) for k, v in update["$set"].items()})
                return

    async def insert_one(self, doc):
        self.docs.append({k: _bson(v) for k, v in doc.items()})


class MongoLikeDB:
    def __init__(self, platform="instagram"):
        self.videos = MongoLikeCollection()
        self.channels = MongoLikeCollection([{"channel_id": "c1", "platform": platform}])
        self.posting_queue = MongoLikeCollection()


@pytest.fixture
def scheduled(monkeypatch):
    calls = []

    async def fake_schedule(*, db, platform, channel_id, video_doc, scheduled_at):
        calls.append(scheduled_at)

    monkeypatch.setattr(ra, "schedule_on_platform", fake_schedule)
    return calls


@pytest.mark.asyncio
async def test_promotion_reads_a_stored_schedule_as_utc(scheduled):
    db = MongoLikeDB()
    await db.videos.insert_one(
        {
            "channel_id": "c1",
            "video_id": "v1",
            "status": "processing",
            "packaging_status": "skipped",
            "scheduled_at": WANT,
        }
    )
    assert db.videos.docs[0]["scheduled_at"] == datetime(2099, 1, 1, 13, 30)  # stored naive UTC
    await ra.promote_processing_to_ready(db, "c1", "v1")
    assert scheduled == [WANT]


class _FakeR2:
    def upload_video(self, _fh, _key):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", ["2099-01-01T19:00:00", "2099-01-01T19:00:00+05:30", "2099-01-01T13:30:00Z"])
async def test_create_video_form_schedule_lands_at_the_time_sent(tmp_path, scheduled, sent):
    from app.services.video_service import VideoService

    db = MongoLikeDB()
    svc = VideoService.__new__(VideoService)
    svc.db, svc.r2, svc.gemini = db, _FakeR2(), object()
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")
    with src.open("rb") as fh:
        await svc.create_video("c1", fh, "t", scheduled_at=sent, analyze=False)
    assert scheduled == [WANT]
