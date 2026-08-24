"""delete_video: library-only by default, opt-in platform deletion on YouTube."""

import pytest

from app.services.video_service import VideoService


def _match(doc, query):
    return all(doc.get(k) == v for k, v in query.items())


class FakeColl:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    async def find_one(self, query):
        return next((d for d in self.docs if _match(d, query)), None)

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if _match(d, query):
                del self.docs[i]
                return

    async def count_documents(self, query):
        return sum(1 for d in self.docs if _match(d, query))


class FakeDB:
    def __init__(self, video, channel):
        self.videos = FakeColl([video] if video else [])
        self.channels = FakeColl([channel] if channel else [])
        self.posting_queue = FakeColl()
        self.schedule_queue = FakeColl()


class FakeR2:
    def __init__(self):
        self.deleted = []

    def delete_video(self, key):
        self.deleted.append(key)


class FakeYouTube:
    def __init__(self, fail=False):
        self.fail = fail
        self.deleted = []

    def delete_video(self, youtube_video_id):
        if self.fail:
            raise RuntimeError("youtube api error")
        self.deleted.append(youtube_video_id)


class FakeYTManager:
    def __init__(self, service):
        self.service = service

    async def get_service(self, channel_id):
        return self.service


def _video(**over):
    doc = {
        "_id": "m1",
        "channel_id": "c1",
        "video_id": "v1",
        "youtube_video_id": "yt1",
        "r2_object_key": "c1/v1.mp4",
    }
    doc.update(over)
    return doc


def _service(db, yt=None):
    return VideoService(db=db, r2_service=FakeR2(), youtube_manager=FakeYTManager(yt) if yt else None)


@pytest.mark.asyncio
async def test_default_delete_leaves_the_platform_untouched():
    yt = FakeYouTube()
    db = FakeDB(_video(), {"channel_id": "c1", "platform": "youtube"})
    svc = _service(db, yt)

    result = await svc.delete_video("c1", "v1")  # no flag

    assert result["deleted"] is True
    assert result["platform_deleted"] is False
    assert yt.deleted == []  # never called
    assert db.videos.docs == []  # local record removed


@pytest.mark.asyncio
async def test_opt_in_deletes_the_youtube_video_then_the_record():
    yt = FakeYouTube()
    db = FakeDB(_video(), {"channel_id": "c1", "platform": "youtube"})
    svc = _service(db, yt)

    result = await svc.delete_video("c1", "v1", delete_on_platform=True)

    assert yt.deleted == ["yt1"]
    assert result["platform_deleted"] is True
    assert db.videos.docs == []


@pytest.mark.asyncio
async def test_platform_failure_aborts_and_keeps_the_record():
    yt = FakeYouTube(fail=True)
    db = FakeDB(_video(), {"channel_id": "c1", "platform": "youtube"})
    svc = _service(db, yt)

    with pytest.raises(RuntimeError):
        await svc.delete_video("c1", "v1", delete_on_platform=True)

    # Record (and youtube_video_id) preserved for a retry.
    assert len(db.videos.docs) == 1


@pytest.mark.asyncio
async def test_missing_youtube_token_aborts():
    db = FakeDB(_video(), {"channel_id": "c1", "platform": "youtube"})
    svc = VideoService(db=db, r2_service=FakeR2(), youtube_manager=FakeYTManager(None))

    with pytest.raises(RuntimeError):
        await svc.delete_video("c1", "v1", delete_on_platform=True)
    assert len(db.videos.docs) == 1


@pytest.mark.asyncio
async def test_unpublished_video_deletes_locally_with_a_note():
    yt = FakeYouTube()
    db = FakeDB(_video(youtube_video_id=None), {"channel_id": "c1", "platform": "youtube"})
    svc = _service(db, yt)

    result = await svc.delete_video("c1", "v1", delete_on_platform=True)

    assert yt.deleted == []
    assert result["platform_deleted"] is False
    assert "not published" in result["platform_error"].lower()
    assert db.videos.docs == []


@pytest.mark.asyncio
async def test_instagram_is_reported_unsupported_but_still_removed_locally():
    db = FakeDB(_video(youtube_video_id=None), {"channel_id": "c1", "platform": "instagram"})
    svc = _service(db, FakeYouTube())

    result = await svc.delete_video("c1", "v1", delete_on_platform=True)

    assert result["platform_deleted"] is False
    assert "instagram" in result["platform_error"].lower()
    assert db.videos.docs == []
