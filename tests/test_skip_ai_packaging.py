"""Uploading or importing with AI packaging switched off.

Analysis is what writes a video's title, description and tags, so every upload
path creates the record as ``processing`` and waits. Declining the AI means
nothing is coming to write that metadata — so the video has to be released
immediately, keeping whatever the uploader typed, rather than waiting forever
for a packaging step that will never run.

Release goes through ``promote_processing_to_ready`` rather than a bare status
write: that is what puts a video on the posting queue, which Schedule All reads.
"""

from __future__ import annotations

import pytest

from app.services.retention_analysis import promote_processing_to_ready


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.inserted: list[dict] = []

    async def find_one(self, query, _projection=None, sort=None):
        matches = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda d: d.get(key, 0), reverse=direction < 0)
        return matches[0] if matches else None

    async def update_one(self, query, update):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update["$set"])
                return

    async def insert_one(self, doc):
        self.inserted.append(doc)
        self.docs.append(doc)


class FakeDB:
    def __init__(self, video, channel=None):
        self.videos = FakeCollection([video])
        self.channels = FakeCollection([channel or {"channel_id": "c1", "platform": "youtube"}])
        self.posting_queue = FakeCollection()


def _video(**extra):
    return {"channel_id": "c1", "video_id": "v1", "status": "processing", **extra}


@pytest.mark.asyncio
async def test_a_skipped_video_is_released_to_ready():
    db = FakeDB(_video(packaging_status="skipped"))
    await promote_processing_to_ready(db, "c1", "v1")
    assert db.videos.docs[0]["status"] == "ready"


@pytest.mark.asyncio
async def test_a_skipped_video_reaches_the_posting_queue():
    """Schedule All walks posting_queue; a bare status flip left it invisible there."""
    db = FakeDB(_video(packaging_status="skipped"))
    await promote_processing_to_ready(db, "c1", "v1")
    assert [e["video_id"] for e in db.posting_queue.inserted] == ["v1"]


@pytest.mark.asyncio
async def test_a_completed_video_still_promotes():
    db = FakeDB(_video(packaging_status="completed"))
    await promote_processing_to_ready(db, "c1", "v1")
    assert db.videos.docs[0]["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("packaging", ["pending", "analyzing", "failed", None])
async def test_a_video_still_awaiting_packaging_is_not_released(packaging):
    """Only 'completed' and 'skipped' are terminal — the rest have metadata coming,
    or produced none, and posting either without it is the failure this prevents."""
    db = FakeDB(_video(packaging_status=packaging))
    await promote_processing_to_ready(db, "c1", "v1")
    assert db.videos.docs[0]["status"] == "processing"
    assert db.posting_queue.inserted == []


@pytest.mark.asyncio
async def test_a_live_video_is_never_rewound():
    """Guards against a re-run promoting something already published."""
    db = FakeDB(_video(status="published", packaging_status="skipped"))
    await promote_processing_to_ready(db, "c1", "v1")
    assert db.videos.docs[0]["status"] == "published"
    assert db.posting_queue.inserted == []


# ------------------------------------------------------------------
# create_video — the manual upload path
# ------------------------------------------------------------------


class _FakeR2:
    def upload_video(self, _fh, _key):
        return None


def _service(db):
    from app.services.video_service import VideoService

    svc = VideoService.__new__(VideoService)
    svc.db = db
    svc.r2 = _FakeR2()
    svc.gemini = object()
    return svc


class _UploadDB(FakeDB):
    def __init__(self, channel=None):
        super().__init__(_video(video_id="unused"), channel)
        self.videos = FakeCollection()


@pytest.mark.asyncio
async def test_skipping_analysis_never_starts_it(tmp_path, monkeypatch):
    db = _UploadDB()
    svc = _service(db)
    started: list = []
    monkeypatch.setattr(
        type(svc), "trigger_retention_analysis", lambda *a, **k: started.append(a), raising=True
    )

    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")
    with src.open("rb") as fh:
        result = await svc.create_video("c1", fh, "My own title", analyze=False)

    assert started == []
    assert result["video"]["packaging_status"] == "skipped"
    assert result["video"]["status"] == "ready"
    assert result["video"]["title"] == "My own title"


@pytest.mark.asyncio
async def test_analysing_is_still_the_default(tmp_path, monkeypatch):
    db = _UploadDB()
    svc = _service(db)
    started: list = []
    monkeypatch.setattr(
        type(svc), "trigger_retention_analysis", lambda *a, **k: started.append(a), raising=True
    )

    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")
    with src.open("rb") as fh:
        result = await svc.create_video("c1", fh, "Placeholder")

    assert len(started) == 1
    assert result["video"]["packaging_status"] == "pending"
    assert result["video"]["status"] == "processing"
