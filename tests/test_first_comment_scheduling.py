"""When a first comment is armed, and when it is allowed to go up.

YouTube is the awkward one: the upload happens now but the video stays private
behind ``publishAt``, and YouTube rejects a comment on a private video. So the
comment cannot simply follow the upload — it waits for the publish time and a
sweep picks it up.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from app.services import youtube_uploader
from app.services.first_comment import PENDING, POSTED
from app.services.video_service import VideoService
from app.timezone import now_ist

# --- the uploader -------------------------------------------------------------


class _YT:
    def __init__(self) -> None:
        self.commented: list[tuple[str, str]] = []

    def upload_video(self, **_kwargs):
        return "yt-1"

    def post_comment(self, video_id, text):
        self.commented.append((video_id, text))
        return "yt-comment-1"


class _R2:
    def file_exists(self, _key):
        return True

    def download_video(self, _key):
        return "/tmp/fake.mp4"


class _Collection:
    async def update_one(self, _flt, _update, **_kw):
        return None

    async def delete_one(self, _flt):
        return None

    async def find_one(self, _flt, *_a, **_k):
        return None


class _DB:
    def __getattr__(self, _name):
        return _Collection()


def _upload(scheduled_at) -> _YT:
    yt = _YT()
    ok = asyncio.run(
        youtube_uploader._upload_one_video(
            db=_DB(),
            r2_service=_R2(),
            youtube_service=yt,
            channel_id="c1",
            video_doc={
                "video_id": "v1",
                "r2_object_key": "c1/v1.mp4",
                "title": "t",
                "first_comment": "first!",
                "first_comment_status": PENDING,
            },
            queue_entry={"_id": "q1", "scheduled_at": scheduled_at},
        )
    )
    assert ok
    return yt


def test_an_immediate_upload_is_commented_on_straight_away():
    """No publishAt means the video is public the moment the upload returns."""
    assert _upload(None).commented == [("yt-1", "first!")]


def test_a_scheduled_upload_leaves_the_comment_for_later():
    """The video is private until publishAt; commenting now would be rejected."""
    assert _upload(now_ist() + timedelta(hours=3)).commented == []


# --- the sweep ----------------------------------------------------------------


class _SweepVideos:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self.updates: list[dict[str, Any]] = []

    def find(self, query):
        now = query["scheduled_at"]["$lte"]
        matched = [
            d
            for d in self._docs
            if d.get("first_comment_status") == query["first_comment_status"]
            and d.get("youtube_video_id") is not None
            and d.get("scheduled_at") is not None
            and d["scheduled_at"] <= now
        ]

        class _Cursor:
            async def to_list(self, length=None):
                return [dict(d) for d in matched]

        return _Cursor()

    async def update_one(self, _flt, update, **_kw):
        self.updates.append(update.get("$set", {}))


class _SweepDB:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.videos = _SweepVideos(docs)


def _sweep(docs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> tuple[_YT, _SweepDB]:
    import app.main as main_app

    yt = _YT()

    class _Manager:
        async def get_service(self, _channel_id):
            return yt

    monkeypatch.setattr(main_app, "youtube_service_manager", _Manager(), raising=False)
    db = _SweepDB(docs)
    asyncio.run(youtube_uploader._post_due_first_comments(db))
    return yt, db


def _due(**overrides: Any) -> dict[str, Any]:
    doc = {
        "channel_id": "c1",
        "video_id": "v1",
        "first_comment": "first!",
        "first_comment_status": PENDING,
        "youtube_video_id": "yt-1",
        "scheduled_at": now_ist() - timedelta(minutes=5),
    }
    doc.update(overrides)
    return doc


def test_the_sweep_posts_a_comment_once_its_publish_time_has_passed(monkeypatch):
    yt, db = _sweep([_due()], monkeypatch)
    assert yt.commented == [("yt-1", "first!")]
    assert db.videos.updates[-1]["first_comment_status"] == POSTED


def test_the_sweep_leaves_a_video_that_has_not_published_yet(monkeypatch):
    yt, _ = _sweep([_due(scheduled_at=now_ist() + timedelta(hours=1))], monkeypatch)
    assert yt.commented == []


def test_the_sweep_ignores_videos_whose_comment_already_went_up(monkeypatch):
    yt, _ = _sweep([_due(first_comment_status=POSTED)], monkeypatch)
    assert yt.commented == []


# --- arming at schedule time --------------------------------------------------


class _ScheduleVideos:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self.sets: list[dict[str, Any]] = []

    async def find_one(self, query, *_a, **_k):
        for d in self._docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d, _id=d["video_id"])
        return None

    async def update_one(self, _flt, update, **_kw):
        self.sets.append(update.get("$set", {}))


class _ScheduleDB:
    def __init__(self, channel: dict[str, Any], videos: list[dict[str, Any]]) -> None:
        self.channels = _Channels(channel)
        self.videos = _ScheduleVideos(videos)
        self.schedule_queue = _Queue()
        self.posting_queue = _Queue()


class _Channels:
    def __init__(self, channel: dict[str, Any]) -> None:
        self._channel = channel

    async def find_one(self, _query, *_a, **_k):
        return dict(self._channel)


class _Queue:
    async def find_one(self, *_a, **_k):
        return None

    async def update_one(self, *_a, **_k):
        return None

    async def delete_one(self, *_a, **_k):
        return None

    async def insert_one(self, *_a, **_k):
        return None

    def find(self, *_a, **_k):
        class _Cursor:
            def sort(self, *_sa, **_sk):
                return self

            async def to_list(self, length=None):
                return []

        return _Cursor()


def _schedule(first_comment: str | None, video_id: str = "v1"):
    db = _ScheduleDB(
        {"channel_id": "c1", "platform": "instagram"},
        [{"channel_id": "c1", "video_id": "v1", "status": "ready", "r2_object_key": "c1/v1.mp4"}],
    )
    service = VideoService(db=db)
    result = asyncio.run(service.schedule_video("c1", video_id, now_ist() + timedelta(hours=1), first_comment))
    return result, db


def test_scheduling_with_a_comment_arms_it_as_pending():
    _, db = _schedule("first!")
    armed = [s for s in db.videos.sets if "first_comment" in s]
    assert armed and armed[0]["first_comment"] == "first!"
    assert armed[0]["first_comment_status"] == PENDING


def test_scheduling_without_a_comment_touches_nothing():
    _, db = _schedule(None)
    assert not [s for s in db.videos.sets if "first_comment" in s]


def test_one_comment_cannot_be_applied_to_a_whole_queue():
    """Scheduling "all" with a comment would put the same text under every
    video — far more likely a mistake than an intention, and tedious to undo."""
    with pytest.raises(ValueError, match="single video"):
        _schedule("first!", video_id="all")


def test_a_comment_too_long_for_the_platform_is_refused_at_schedule_time():
    with pytest.raises(ValueError, match="2200"):
        _schedule("x" * 2201)


# --- rescheduling -------------------------------------------------------------


def _reschedule(first_comment: str | None, existing: dict[str, Any] | None = None):
    db = _ScheduleDB(
        {"channel_id": "c1", "platform": "instagram"},
        [
            {
                "channel_id": "c1",
                "video_id": "v1",
                "status": "queued",
                "first_comment": "already set",
                **(existing or {}),
            }
        ],
    )
    service = VideoService(db=db)
    asyncio.run(service.reschedule_video("c1", "v1", now_ist() + timedelta(hours=2), first_comment))
    return db


def test_rescheduling_can_set_a_comment():
    db = _reschedule("new comment")
    assert db.videos.sets[0]["first_comment"] == "new comment"
    assert db.videos.sets[0]["first_comment_status"] == PENDING


def test_rescheduling_without_mentioning_the_comment_leaves_it_alone():
    """A plain time change must not wipe a comment armed when the video was
    first scheduled."""
    db = _reschedule(None)
    assert "first_comment" not in db.videos.sets[0]


def test_rescheduling_with_an_empty_comment_clears_it():
    """An emptied box is an explicit "no comment", unlike an absent field."""
    db = _reschedule("")
    assert db.videos.sets[0]["first_comment"] is None
    assert db.videos.sets[0]["first_comment_status"] is None
