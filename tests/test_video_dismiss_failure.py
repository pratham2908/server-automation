"""VideoService.dismiss_failure and the status-change marker clearing."""

from types import SimpleNamespace

import pytest

from app.services.video_service import VideoService


class FakeVideos:
    """In-memory videos collection supporting the $set/$unset paths under test."""

    def __init__(self, docs):
        self._docs = [dict(d) for d in docs]

    @staticmethod
    def _matches(doc, query):
        return all(doc.get(k) == v for k, v in query.items())

    async def find_one(self, query, projection=None):
        for doc in self._docs:
            if self._matches(doc, query):
                return dict(doc)
        return None

    async def update_one(self, query, update):
        for doc in self._docs:
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                for field in update.get("$unset", {}):
                    doc.pop(field, None)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def delete_one(self, query):
        return SimpleNamespace(deleted_count=0)

    def get(self, video_id):
        return next((d for d in self._docs if d.get("video_id") == video_id), None)


class FakeDB:
    def __init__(self, videos):
        self.videos = FakeVideos(videos)
        self.posting_queue = FakeVideos([])
        self.schedule_queue = FakeVideos([])


def _service(videos):
    db = FakeDB(videos)
    return VideoService(db), db


_MARKER = {"stage": "publish", "platform": "instagram", "reason": "Platform quota or rate limit reached"}


@pytest.mark.asyncio
async def test_dismiss_removes_the_marker_but_keeps_the_video_ready():
    service, db = _service([{"_id": "1", "channel_id": "c", "video_id": "v", "status": "ready", "last_failure": _MARKER}])

    result = await service.dismiss_failure("c", "v")

    assert result == {"ok": True, "video_id": "v"}
    stored = db.videos.get("v")
    assert "last_failure" not in stored
    assert stored["status"] == "ready"


@pytest.mark.asyncio
async def test_dismiss_is_idempotent_when_there_is_no_marker():
    service, db = _service([{"_id": "1", "channel_id": "c", "video_id": "v", "status": "ready"}])

    result = await service.dismiss_failure("c", "v")

    assert result["ok"] is True
    assert "last_failure" not in db.videos.get("v")


@pytest.mark.asyncio
async def test_dismiss_on_a_missing_video_raises():
    service, _ = _service([])
    with pytest.raises(ValueError, match="not found"):
        await service.dismiss_failure("c", "nope")


@pytest.mark.asyncio
async def test_moving_a_bounced_video_off_ready_clears_the_stale_marker():
    service, db = _service(
        [{"_id": "1", "channel_id": "c", "video_id": "v", "status": "ready", "last_failure": _MARKER}]
    )

    await service.update_video_status("c", "v", "todo")

    stored = db.videos.get("v")
    assert stored["status"] == "todo"
    assert "last_failure" not in stored
