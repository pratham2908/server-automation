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

    async def update_one(self, query, update):
        for d in self.docs:
            if _match(d, query):
                for k, v in update.get("$set", {}).items():
                    d[k] = v
                for k in update.get("$unset", {}):
                    d.pop(k, None)
                return

    async def count_documents(self, query):
        return sum(1 for d in self.docs if _match(d, query))


def _archived(db):
    """delete_video now soft-deletes: the record stays as an archived tombstone."""
    return len(db.videos.docs) == 1 and db.videos.docs[0].get("status") == "archived"


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


class FakeInstagram:
    def __init__(self, fail=None):
        # fail: None (ok) | "value" (provider not supported) | "http" (api error)
        self.fail = fail
        self.deleted = []

    def delete_media(self, media_id):
        if self.fail == "value":
            raise ValueError("Instagram media deletion is only supported for Facebook Login")
        if self.fail == "http":
            raise RuntimeError("graph api error")
        self.deleted.append(media_id)


class FakeIGManager:
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


def _service(db, yt=None, ig=None):
    return VideoService(
        db=db,
        r2_service=FakeR2(),
        youtube_manager=FakeYTManager(yt) if yt else None,
        instagram_manager=FakeIGManager(ig) if ig else None,
    )


@pytest.mark.asyncio
async def test_default_delete_leaves_the_platform_untouched():
    yt = FakeYouTube()
    db = FakeDB(_video(), {"channel_id": "c1", "platform": "youtube"})
    svc = _service(db, yt)

    result = await svc.delete_video("c1", "v1")  # no flag

    assert result["deleted"] is True
    assert result["platform_deleted"] is False
    assert yt.deleted == []  # never called
    assert _archived(db)  # soft-deleted: tombstone kept


@pytest.mark.asyncio
async def test_opt_in_deletes_the_youtube_video_then_the_record():
    yt = FakeYouTube()
    db = FakeDB(_video(), {"channel_id": "c1", "platform": "youtube"})
    svc = _service(db, yt)

    result = await svc.delete_video("c1", "v1", delete_on_platform=True)

    assert yt.deleted == ["yt1"]
    assert result["platform_deleted"] is True
    assert _archived(db)


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
    assert _archived(db)


@pytest.mark.asyncio
async def test_opt_in_deletes_the_instagram_media_then_the_record():
    ig = FakeInstagram()
    db = FakeDB(
        _video(youtube_video_id=None, instagram_media_id="ig1"),
        {"channel_id": "c1", "platform": "instagram"},
    )
    svc = _service(db, ig=ig)

    result = await svc.delete_video("c1", "v1", delete_on_platform=True)

    assert ig.deleted == ["ig1"]
    assert result["platform_deleted"] is True
    assert _archived(db)


@pytest.mark.asyncio
async def test_instagram_login_channel_cannot_delete_but_removed_locally():
    ig = FakeInstagram(fail="value")  # provider-not-supported → ValueError
    db = FakeDB(
        _video(youtube_video_id=None, instagram_media_id="ig1"),
        {"channel_id": "c1", "platform": "instagram"},
    )
    svc = _service(db, ig=ig)

    result = await svc.delete_video("c1", "v1", delete_on_platform=True)

    assert result["platform_deleted"] is False
    assert "facebook login" in result["platform_error"].lower()
    assert _archived(db)  # local soft-delete still proceeds


@pytest.mark.asyncio
async def test_instagram_api_failure_aborts_and_keeps_the_record():
    ig = FakeInstagram(fail="http")
    db = FakeDB(
        _video(youtube_video_id=None, instagram_media_id="ig1"),
        {"channel_id": "c1", "platform": "instagram"},
    )
    svc = _service(db, ig=ig)

    with pytest.raises(RuntimeError):
        await svc.delete_video("c1", "v1", delete_on_platform=True)
    assert len(db.videos.docs) == 1  # kept for retry


@pytest.mark.asyncio
async def test_unpublished_instagram_video_deletes_locally_with_a_note():
    db = FakeDB(
        _video(youtube_video_id=None),  # no instagram_media_id
        {"channel_id": "c1", "platform": "instagram"},
    )
    svc = _service(db, ig=FakeInstagram())

    result = await svc.delete_video("c1", "v1", delete_on_platform=True)

    assert result["platform_deleted"] is False
    assert "not published" in result["platform_error"].lower()
    assert _archived(db)


@pytest.mark.asyncio
async def test_soft_delete_records_provenance_and_clears_r2():
    yt = FakeYouTube()
    r2 = FakeR2()
    db = FakeDB(_video(status="published"), {"channel_id": "c1", "platform": "youtube"})
    svc = VideoService(db=db, r2_service=r2, youtube_manager=FakeYTManager(yt))

    await svc.delete_video("c1", "v1", delete_on_platform=True)

    doc = db.videos.docs[0]
    assert doc["status"] == "archived"
    assert doc["archived_from_status"] == "published"
    assert doc["platform_deleted"] is True
    assert doc["r2_object_key"] is None  # uploaded copy purged
    assert r2.deleted == ["c1/v1.mp4"]


@pytest.mark.asyncio
async def test_restore_returns_video_to_previous_status():
    db = FakeDB(_video(status="ready"), {"channel_id": "c1", "platform": "youtube"})
    svc = _service(db, FakeYouTube())

    await svc.delete_video("c1", "v1")  # archive (local only)
    assert db.videos.docs[0]["status"] == "archived"

    result = await svc.restore_video("c1", "v1")

    assert result["restored"] is True
    assert result["status"] == "ready"
    assert db.videos.docs[0]["status"] == "ready"
    assert "archived_at" not in db.videos.docs[0]


@pytest.mark.asyncio
async def test_restore_rejects_a_non_archived_video():
    db = FakeDB(_video(status="published"), {"channel_id": "c1", "platform": "youtube"})
    svc = _service(db, FakeYouTube())
    with pytest.raises(ValueError):
        await svc.restore_video("c1", "v1")
