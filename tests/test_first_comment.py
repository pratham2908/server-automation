"""The comment that goes up with the video.

People ask for this as a "pinned comment", but neither platform lets an API pin
anything — Instagram's comment node supports read, delete and hide only. Posting
first is the achievable part, so these tests are about the comment landing at
the right moment and never taking a successful publish down with it.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from app.services import auto_publisher, youtube_uploader
from app.services.first_comment import (
    FAILED,
    MAX_ATTEMPTS,
    PENDING,
    POSTED,
    is_due,
    pending_fields,
    post_and_record,
    validate_comment,
)
from app.services.instagram import InstagramService
from app.services.youtube import YouTubeService
from app.timezone import now_ist

# --- validation ---------------------------------------------------------------


def test_a_blank_comment_means_no_comment_rather_than_an_error():
    """The field is optional, so an empty box is not a failed request."""
    assert validate_comment(None, "instagram") is None
    assert validate_comment("   \n ", "instagram") is None


def test_surrounding_whitespace_is_stripped_and_line_breaks_normalised():
    assert validate_comment("  hi\r\nthere  ", "instagram") == "hi\nthere"


def test_a_comment_past_the_instagram_limit_is_refused():
    with pytest.raises(ValueError, match="2200"):
        validate_comment("x" * 2201, "instagram")


def test_youtube_allows_a_longer_comment_than_instagram():
    """2200 is Instagram's ceiling, not a universal one — refusing it for
    YouTube would reject text YouTube accepts."""
    assert validate_comment("x" * 2201, "youtube") is not None
    with pytest.raises(ValueError, match="10000"):
        validate_comment("x" * 10001, "youtube")


def test_arming_and_clearing_are_both_expressible():
    armed = pending_fields("hello")
    assert armed["first_comment"] == "hello"
    assert armed["first_comment_status"] == PENDING

    cleared = pending_fields(None)
    assert cleared["first_comment"] is None
    assert cleared["first_comment_status"] is None


def test_rearming_resets_the_attempt_count():
    """Otherwise a rescheduled video inherits the failed run's attempts and
    gives up immediately."""
    assert pending_fields("hello")["first_comment_attempts"] == 0


# --- when a pending comment is due --------------------------------------------


def _pending(**overrides: Any) -> dict[str, Any]:
    doc = {
        "video_id": "v1",
        "first_comment": "first!",
        "first_comment_status": PENDING,
        "youtube_video_id": "yt1",
        "scheduled_at": now_ist() - timedelta(minutes=1),
    }
    doc.update(overrides)
    return doc


def test_a_pending_comment_whose_time_has_passed_is_due():
    assert is_due(_pending(), "youtube", now_ist())


def test_a_video_still_waiting_for_its_publish_time_is_not_due():
    """YouTube holds it private until publishAt, and rejects comments on a
    private video — posting early would just burn an attempt."""
    assert not is_due(_pending(scheduled_at=now_ist() + timedelta(hours=2)), "youtube", now_ist())


def test_an_already_posted_comment_is_not_posted_again():
    assert not is_due(_pending(first_comment_status=POSTED), "youtube", now_ist())


def test_a_video_with_no_platform_id_yet_is_not_due():
    assert not is_due(_pending(youtube_video_id=None), "youtube", now_ist())


def test_a_comment_that_has_exhausted_its_attempts_is_not_due():
    assert not is_due(_pending(first_comment_attempts=MAX_ATTEMPTS), "youtube", now_ist())


# --- posting and recording ----------------------------------------------------


class _Videos:
    def __init__(self) -> None:
        self.doc: dict[str, Any] = {}

    async def update_one(self, _flt, update, **_kw):
        self.doc.update(update.get("$set", {}))


class _DB:
    def __init__(self) -> None:
        self.videos = _Videos()

    def __getattr__(self, _name):
        raise AttributeError(_name)


def _record(post, **overrides: Any) -> tuple[bool, dict[str, Any]]:
    db = _DB()
    ok = asyncio.run(
        post_and_record(
            db=db,
            post=post,
            channel_id="c1",
            video_doc=_pending(**overrides),
            platform="youtube",
        )
    )
    return ok, db.videos.doc


def test_a_posted_comment_records_its_id():
    ok, doc = _record(lambda _vid, _text: "comment-1")
    assert ok
    assert doc["first_comment_status"] == POSTED
    assert doc["first_comment_id"] == "comment-1"


def test_the_comment_text_reaches_the_platform_unchanged():
    seen: dict[str, str] = {}

    def post(video_id, text):
        seen["video_id"], seen["text"] = video_id, text
        return "c"

    _record(post)
    assert seen == {"video_id": "yt1", "text": "first!"}


def test_a_failure_stays_pending_so_the_sweep_retries():
    def boom(_vid, _text):
        raise RuntimeError("rate limited")

    ok, doc = _record(boom)
    assert not ok
    assert doc["first_comment_status"] == PENDING
    assert doc["first_comment_attempts"] == 1
    assert "rate limited" in doc["first_comment_error"]


def test_the_last_attempt_gives_up_rather_than_retrying_forever():
    def boom(_vid, _text):
        raise RuntimeError("nope")

    _, doc = _record(boom, first_comment_attempts=MAX_ATTEMPTS - 1)
    assert doc["first_comment_status"] == FAILED


def test_a_platform_failure_never_propagates():
    """The video is already live; an exception here would bounce a successful
    publish back into the retry path."""

    def boom(_vid, _text):
        raise RuntimeError("nope")

    ok, _ = _record(boom)
    assert ok is False


# --- the platform calls -------------------------------------------------------


def test_instagram_posts_to_the_media_comments_edge():
    svc = InstagramService(access_token="t")
    sent: dict[str, Any] = {}

    def fake_post(endpoint, params=None):
        sent["endpoint"], sent["params"] = endpoint, params
        return {"id": "ig-comment-1"}

    svc._post = fake_post  # type: ignore[method-assign]

    assert svc.post_comment("media-9", "hello") == "ig-comment-1"
    assert sent["endpoint"] == "media-9/comments"
    assert sent["params"] == {"message": "hello"}


def test_youtube_posts_a_top_level_comment_thread():
    svc = YouTubeService.__new__(YouTubeService)
    captured: dict[str, Any] = {}

    class _Threads:
        def insert(self, **kwargs):
            captured.update(kwargs)
            return "request"

    svc._youtube = type("Y", (), {"commentThreads": lambda _self: _Threads()})()  # type: ignore[attr-defined]
    svc._execute = lambda _request: {"id": "yt-comment-1"}  # type: ignore[method-assign]

    assert svc.post_comment("vid-9", "hello") == "yt-comment-1"
    snippet = captured["body"]["snippet"]
    assert snippet["videoId"] == "vid-9"
    assert snippet["topLevelComment"]["snippet"]["textOriginal"] == "hello"


# --- the publish paths --------------------------------------------------------


class _Collection:
    async def update_one(self, _flt, _update, **_kw):
        return None

    async def delete_one(self, _flt):
        return None

    async def insert_one(self, _doc):
        return None

    async def find_one(self, _flt, *_a, **_k):
        return None


class _WorkerDB:
    def __init__(self) -> None:
        self.videos_updates: list[dict[str, Any]] = []

    def __getattr__(self, name):
        if name == "videos":
            outer = self

            class _V(_Collection):
                async def update_one(self, _flt, update, **_kw):
                    outer.videos_updates.append(update.get("$set", {}))

            return _V()
        return _Collection()


class _R2:
    def file_exists(self, _key):
        return True

    def generate_presigned_url(self, key, expires_in=3600):
        return f"https://r2/{key}"


class _IG:
    def __init__(self, comment_id="ig-1", fail=False) -> None:
        self.comment_id, self.fail = comment_id, fail
        self.commented: list[tuple[str, str]] = []

    def publish_reel_from_url(self, **_kwargs):
        return "media-1"

    def post_comment(self, media_id, message):
        if self.fail:
            raise RuntimeError("comments disabled")
        self.commented.append((media_id, message))
        return self.comment_id


def _publish_reel(video_extra: dict[str, Any], ig: _IG) -> tuple[bool, _WorkerDB]:
    db = _WorkerDB()
    ok = asyncio.run(
        auto_publisher._publish_one_reel(
            db=db,
            r2_service=_R2(),
            instagram_service=ig,
            channel_doc={"channel_id": "c1", "instagram_user_id": "ig1"},
            video_doc={"video_id": "v1", "r2_object_key": "c1/v1.mp4", "title": "t", **video_extra},
            queue_entry={"_id": "q1"},
        )
    )
    return ok, db


def test_instagram_comments_on_the_reel_it_just_published():
    ig = _IG()
    ok, _ = _publish_reel({"first_comment": "first!", "first_comment_status": PENDING}, ig)
    assert ok
    # The media id comes from this publish, not from the stale doc.
    assert ig.commented == [("media-1", "first!")]


def test_a_reel_without_a_first_comment_posts_nothing():
    ig = _IG()
    _publish_reel({}, ig)
    assert ig.commented == []


def test_a_failed_comment_does_not_fail_the_publish():
    ok, db = _publish_reel({"first_comment": "first!", "first_comment_status": PENDING}, _IG(fail=True))
    assert ok
    assert any(u.get("status") == "published" for u in db.videos_updates)
    assert any(u.get("first_comment_status") == PENDING for u in db.videos_updates)


# --- commenting on an already-published video ---------------------------------


class _ManualVideos:
    def __init__(self, video: dict[str, Any] | None) -> None:
        self._video = video

    async def find_one(self, _query, *_a, **_k):
        return dict(self._video) if self._video else None


class _ManualChannels:
    def __init__(self, channel: dict[str, Any]) -> None:
        self._channel = channel

    async def find_one(self, _query, *_a, **_k):
        return dict(self._channel)


class _ManualDB:
    def __init__(self, channel, video) -> None:
        self.channels = _ManualChannels(channel)
        self.videos = _ManualVideos(video)


def _comment_on(video: dict[str, Any] | None, platform: str = "instagram", service: Any = None):
    from app.services.video_service import VideoService

    class _Manager:
        async def get_service(self, _channel_id):
            return service

    svc = VideoService(
        db=_ManualDB({"channel_id": "c1", "platform": platform}, video),
        youtube_manager=_Manager(),
        instagram_manager=_Manager(),
    )
    return asyncio.run(svc.post_video_comment("c1", "v1", "hello"))


def test_a_live_video_can_be_commented_on_by_hand():
    ig = _IG(comment_id="ig-9")
    result = _comment_on({"video_id": "v1", "instagram_media_id": "media-9"}, service=ig)
    assert result["comment_id"] == "ig-9"
    assert ig.commented == [("media-9", "hello")]


def test_commenting_on_a_video_that_is_not_live_yet_is_refused():
    """There is nothing on the platform to comment on — a clearer failure than
    whatever Meta returns for a null media id."""
    with pytest.raises(ValueError, match="not live"):
        _comment_on({"video_id": "v1"}, service=_IG())


def test_commenting_on_a_missing_video_is_refused():
    with pytest.raises(ValueError, match="Video not found"):
        _comment_on(None, service=_IG())
