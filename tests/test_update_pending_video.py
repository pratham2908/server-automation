"""Editing a queued or scheduled video in place: one call, only the fields sent, and YouTube first.

What matters: a field left out must not be wiped; a scheduled YouTube video's title, description, tags and
time go to YouTube in a single call (``videos.update`` replaces the part it is sent, so the rest of the
snippet and status must survive); and when YouTube refuses, our record must not move.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from googleapiclient.errors import HttpError

from app.services.video_service import VideoService
from app.services.youtube import YouTubeService
from app.services.youtube_schedule import apply_snippet_changes, check_snippet_limits, merged_tags
from app.timezone import now_ist

# --- the pure rules -----------------------------------------------------------


def test_append_keeps_order_and_drops_duplicates_and_replace_discards_the_old_ones():
    assert merged_tags(["a", "b"], ["b", "c"], "append") == ["a", "b", "c"]
    assert merged_tags(["a", "b"], ["c"], "replace") == ["c"]
    assert merged_tags(None, ["x", "x"], None) == ["x"]


@pytest.mark.parametrize(
    ("title", "description", "tags", "message"),
    [
        ("", None, None, "cannot be empty"),
        ("x" * 101, None, None, "at most 100"),
        (None, "é" * 2501, None, "5000 bytes"),
        (None, None, ["t" * 300, "u" * 300], "500 characters"),
    ],
)
def test_a_value_youtube_would_refuse_is_refused_with_a_reason(title, description, tags, message):
    with pytest.raises(ValueError, match=message):
        check_snippet_limits(title, description, tags)


def test_the_snippet_keeps_its_writable_fields_and_drops_the_read_only_ones():
    current = {
        "title": "Old",
        "description": "d",
        "categoryId": "22",
        "tags": ["a"],
        "publishedAt": "2026-10-01T00:00:00Z",
        "thumbnails": {"default": {}},
        "channelId": "UC1",
    }
    updated = apply_snippet_changes(current, title="New")
    assert updated == {"title": "New", "description": "d", "categoryId": "22", "tags": ["a"]}
    assert current["title"] == "Old"


# --- the YouTube call ---------------------------------------------------------


class _Request:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


class _Videos:
    def __init__(self, item: dict[str, Any] | None, log: list[tuple[str, dict[str, Any]]]) -> None:
        self._item, self._log = item, log

    def list(self, **kwargs: Any) -> _Request:
        self._log.append(("list", kwargs))
        return _Request({"items": [self._item] if self._item is not None else []})

    def update(self, **kwargs: Any) -> _Request:
        self._log.append(("update", kwargs))
        return _Request({"status": kwargs["body"].get("status", {})})


class _Client:
    def __init__(self, item: dict[str, Any] | None) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []
        self._item = item

    def videos(self) -> _Videos:
        return _Videos(self._item, self.log)


PRIVATE = {
    "status": {"privacyStatus": "private", "publishAt": "2026-11-05T00:00:00Z", "embeddable": True},
    "snippet": {"title": "Old", "description": "d", "categoryId": "22", "publishedAt": "x"},
}


def _yt(item: dict[str, Any] | None) -> tuple[YouTubeService, _Client]:
    client = _Client(item)
    service = object.__new__(YouTubeService)  # skips OAuth; only update_scheduled_video is exercised
    service._youtube = client  # type: ignore[attr-defined]
    service._execute = lambda request: request.payload  # type: ignore[attr-defined,method-assign]
    return service, client


def test_time_and_text_go_to_youtube_in_one_update_with_the_rest_intact():
    service, client = _yt(PRIVATE)
    service.update_scheduled_video("yt1", publish_at="2026-11-06T09:00:00Z", title="New")
    updates = [call for name, call in client.log if name == "update"]
    assert len(updates) == 1
    assert updates[0]["part"] == "snippet,status"
    body = updates[0]["body"]
    assert body["status"] == {"privacyStatus": "private", "publishAt": "2026-11-06T09:00:00Z", "embeddable": True}
    assert body["snippet"] == {"title": "New", "description": "d", "categoryId": "22"}


def test_a_text_only_change_does_not_touch_the_schedule():
    service, client = _yt({**PRIVATE, "status": {"privacyStatus": "public"}})
    service.update_scheduled_video("yt1", description="fresh")
    (update,) = [call for name, call in client.log if name == "update"]
    assert update["part"] == "snippet" and "status" not in update["body"]


def test_a_new_time_on_a_video_that_is_already_public_is_refused_before_any_update():
    service, client = _yt({**PRIVATE, "status": {"privacyStatus": "public"}})
    with pytest.raises(ValueError, match="already public"):
        service.update_scheduled_video("yt1", publish_at="2026-11-06T09:00:00Z")
    assert [name for name, _ in client.log] == ["list"]


# --- the service --------------------------------------------------------------


class _Videos2:
    def __init__(self, doc: dict[str, Any] | None) -> None:
        self.doc = doc
        self.sets: list[dict[str, Any]] = []

    async def find_one(self, *_a: Any, **_k: Any) -> dict[str, Any] | None:
        return dict(self.doc, _id="oid") if self.doc else None

    async def update_one(self, _flt: Any, update: dict[str, Any], **_k: Any) -> None:
        self.sets.append(update["$set"])


class _Queue:
    def __init__(self) -> None:
        self.sets: list[dict[str, Any]] = []

    async def update_one(self, _flt: Any, update: dict[str, Any], **_k: Any) -> None:
        self.sets.append(update["$set"])


class _Channels:
    def __init__(self, platform: str) -> None:
        self._platform = platform

    async def find_one(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        return {"channel_id": "c1", "platform": self._platform}


class _Db:
    def __init__(self, doc: dict[str, Any] | None, platform: str = "youtube") -> None:
        self.videos = _Videos2(doc)
        self.schedule_queue = _Queue()
        self.channels = _Channels(platform)


class _Yt:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._error = error

    def update_scheduled_video(self, youtube_video_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"id": youtube_video_id, **kwargs})
        if self._error:
            raise self._error
        return {"publish_at": kwargs.get("publish_at")}


class _Manager:
    def __init__(self, service: _Yt | None) -> None:
        self._service = service

    async def get_service(self, _channel_id: str) -> _Yt | None:
        return self._service


QUEUED = {"channel_id": "c1", "video_id": "v1", "status": "queued", "tags": ["a"], "title": "Keep"}
SCHEDULED = {
    "channel_id": "c1",
    "video_id": "v1",
    "status": "scheduled",
    "youtube_video_id": "yt1",
    "tags": ["a"],
    "scheduled_at": now_ist() + timedelta(days=1),
}


def _run(doc: dict[str, Any] | None, yt: _Yt | None = None, platform: str = "youtube", **fields: Any):
    db = _Db(doc, platform)
    service = VideoService(db=db, youtube_manager=_Manager(yt))  # type: ignore[arg-type]
    return asyncio.run(service.update_pending_video("c1", "v1", **fields)), db


def test_a_queued_video_changes_only_what_was_sent_and_its_queue_entry_follows_the_time():
    when = now_ist() + timedelta(days=2)
    result, db = _run(QUEUED, scheduled_at=when, description="new words")
    (saved,) = db.videos.sets
    assert saved["scheduled_at"] == when and saved["description"] == "new words"
    assert "title" not in saved and "tags" not in saved  # left out, so left alone
    assert db.schedule_queue.sets == [{"scheduled_at": when}]
    assert result["ok"]


def test_a_caption_only_edit_does_not_move_the_queue_entry():
    _, db = _run(QUEUED, description="new words")
    assert db.schedule_queue.sets == []


def test_append_merges_tags_with_the_existing_ones():
    _, db = _run(QUEUED, tags=["b"], tag_mode="append")
    assert db.videos.sets[0]["tags"] == ["a", "b"]


def test_a_scheduled_youtube_video_is_changed_on_youtube_first_in_one_call():
    yt = _Yt()
    when = now_ist() + timedelta(days=3)
    result, db = _run(SCHEDULED, yt, scheduled_at=when, title="Fresh title", tags=["z"], tag_mode="append")
    (call,) = yt.calls
    assert call["id"] == "yt1" and call["title"] == "Fresh title" and call["tags"] == ["a", "z"]
    assert call["publish_at"].endswith("Z")
    saved = db.videos.sets[0]
    assert saved["title"] == "Fresh title" and saved["scheduled_at"] == when and saved["tags"] == ["a", "z"]
    assert result["youtube_publish_at"] == call["publish_at"]


def test_changing_only_our_own_category_does_not_call_youtube():
    yt = _Yt()
    _, db = _run(SCHEDULED, yt, category="Tutorials")
    assert yt.calls == [] and db.videos.sets[0]["category"] == "Tutorials"


def test_when_youtube_refuses_our_record_is_left_untouched():
    class _HttpRefusedError(HttpError):
        def __init__(self) -> None:
            self.reason = "invalid title"

    db = _Db(SCHEDULED)
    service = VideoService(db=db, youtube_manager=_Manager(_Yt(_HttpRefusedError())))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="YouTube refused the change"):
        asyncio.run(service.update_pending_video("c1", "v1", title="Bad"))
    assert db.videos.sets == []


@pytest.mark.parametrize("status", ["published", "todo", "ready", "failed"])
def test_only_a_queued_or_scheduled_video_can_be_updated(status: str):
    with pytest.raises(ValueError, match="queued or scheduled"):
        _run({**QUEUED, "status": status}, title="x")


def test_a_request_that_changes_nothing_is_refused():
    with pytest.raises(ValueError, match="at least one field"):
        _run(QUEUED)


def test_an_instagram_video_cannot_be_updated_in_the_scheduled_state():
    with pytest.raises(ValueError, match="Instagram posts are held in our queue"):
        _run({**SCHEDULED}, _Yt(), platform="instagram", title="x")


def test_a_title_over_the_limit_is_refused_before_anything_is_saved():
    db = _Db(QUEUED)
    service = VideoService(db=db, youtube_manager=_Manager(None))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at most 100"):
        asyncio.run(service.update_pending_video("c1", "v1", title="x" * 101))
    assert db.videos.sets == []


def test_a_queued_reels_collaborators_are_set_cleaned_and_cleared_and_left_alone_when_omitted():
    _, db = _run(QUEUED, platform="instagram", collaborators=["@MehrKaul"])
    assert db.videos.sets == [{"collaborators": ["mehrkaul"], "updated_at": db.videos.sets[0]["updated_at"]}]
    _, db = _run(QUEUED, platform="instagram", collaborators=[])
    assert db.videos.sets[0]["collaborators"] == []
    _, db = _run(QUEUED, platform="instagram", description="x")
    assert "collaborators" not in db.videos.sets[0]
    with pytest.raises(ValueError, match="Instagram"):
        _run(QUEUED, platform="youtube", collaborators=["mehrkaul"])
