"""Moving a scheduled YouTube video to a new time.

What matters, because each is expensive when wrong: the time YouTube holds and the time we show must not
disagree; a rejected call must not move our record; the rest of the video's status must survive the update
(``videos.update`` replaces the part it is sent); and a video that has already gone public must be refused,
not quietly altered.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from app.exceptions import ChannelNotConnectedError
from app.services.video_service import VideoService
from app.services.youtube import YouTubeService
from app.services.youtube_schedule import (
    MIN_LEAD,
    apply_publish_at,
    check_reschedulable,
    error_reason,
    publish_at_for,
    to_utc,
)
from app.timezone import now_ist

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)

# --- the pure rules -----------------------------------------------------------


def test_the_time_is_sent_to_youtube_as_utc_with_a_z():
    assert publish_at_for(datetime(2026, 10, 13, 18, 30, tzinfo=UTC), NOW) == "2026-10-13T18:30:00Z"


def test_a_time_given_in_ist_is_converted_not_mislabelled():
    """18:30 IST is 13:00 UTC; sending 18:30Z would publish five and a half hours late."""
    assert publish_at_for(datetime(2026, 10, 13, 18, 30, tzinfo=IST), NOW) == "2026-10-13T13:00:00Z"


def test_a_naive_time_is_read_as_utc_matching_what_mongo_returns():
    assert to_utc(datetime(2026, 10, 13, 18, 30)) == datetime(2026, 10, 13, 18, 30, tzinfo=UTC)


@pytest.mark.parametrize("delta", [timedelta(hours=-3), timedelta(0), timedelta(seconds=30)])
def test_a_past_or_immediate_time_is_refused_instead_of_being_treated_as_now(delta: timedelta):
    with pytest.raises(ValueError, match="at least a minute"):
        publish_at_for(NOW + delta, NOW)


def test_the_margin_is_inclusive():
    assert publish_at_for(NOW + MIN_LEAD, NOW).endswith("Z")


def test_only_a_private_video_can_be_rescheduled():
    check_reschedulable({"privacyStatus": "private", "publishAt": "2026-10-13T00:00:00Z"})
    check_reschedulable({"privacyStatus": "private"})  # held private with no time yet
    for state in ("public", "unlisted"):
        with pytest.raises(ValueError, match=f"already {state}"):
            check_reschedulable({"privacyStatus": state})


def test_the_rest_of_the_status_survives_and_the_input_is_not_mutated():
    original = {"privacyStatus": "private", "embeddable": True, "license": "youtube", "selfDeclaredMadeForKids": False}
    updated = apply_publish_at(original, "2026-10-13T00:00:00Z")
    assert updated == {**original, "publishAt": "2026-10-13T00:00:00Z"}
    assert "publishAt" not in original


def test_googles_reason_is_used_when_it_is_there():
    class _Err(Exception):
        reason = "The scheduled publishing time must be in the future"

    assert error_reason(_Err("<HttpError 400 url=...>")) == "The scheduled publishing time must be in the future"
    assert error_reason(ValueError("plain")) == "plain"


# --- the YouTube call ---------------------------------------------------------


class _Request:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


class _Videos:
    def __init__(self, status: dict[str, Any] | None, log: list[tuple[str, dict[str, Any]]]) -> None:
        self._status, self._log = status, log

    def list(self, **kwargs: Any) -> _Request:
        self._log.append(("list", kwargs))
        return _Request({"items": [{"status": self._status}] if self._status is not None else []})

    def update(self, **kwargs: Any) -> _Request:
        self._log.append(("update", kwargs))
        return _Request({"status": kwargs["body"]["status"]})


class _Client:
    def __init__(self, status: dict[str, Any] | None) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []
        self._status = status

    def videos(self) -> _Videos:
        return _Videos(self._status, self.log)


def _yt(status: dict[str, Any] | None) -> tuple[YouTubeService, _Client]:
    client = _Client(status)
    service = object.__new__(YouTubeService)  # skips OAuth; only reschedule_video is exercised
    service._youtube = client  # type: ignore[attr-defined]
    service._execute = lambda request: request.payload  # type: ignore[attr-defined,method-assign]
    return service, client


def test_a_private_video_gets_the_new_time_with_the_rest_of_its_status_intact():
    service, client = _yt(
        {"privacyStatus": "private", "publishAt": "2026-10-13T00:00:00Z", "embeddable": True, "license": "youtube"}
    )
    applied = service.reschedule_video("yt1", "2026-10-14T09:00:00Z")
    assert applied == "2026-10-14T09:00:00Z"
    (_, update) = next(entry for entry in client.log if entry[0] == "update")
    assert update["part"] == "status" and update["body"]["id"] == "yt1"
    assert update["body"]["status"] == {
        "privacyStatus": "private",
        "publishAt": "2026-10-14T09:00:00Z",
        "embeddable": True,
        "license": "youtube",
    }


def test_a_video_that_is_already_public_is_never_updated():
    service, client = _yt({"privacyStatus": "public"})
    with pytest.raises(ValueError, match="already public"):
        service.reschedule_video("yt1", "2026-10-14T09:00:00Z")
    assert [name for name, _ in client.log] == ["list"]


def test_a_video_youtube_does_not_know_is_reported_not_swallowed():
    service, _ = _yt(None)
    with pytest.raises(ValueError, match="no video"):
        service.reschedule_video("gone", "2026-10-14T09:00:00Z")


# --- the service --------------------------------------------------------------


class _Videos2:
    def __init__(self, doc: dict[str, Any] | None) -> None:
        self.doc = doc
        self.sets: list[dict[str, Any]] = []

    async def find_one(self, *_a: Any, **_k: Any) -> dict[str, Any] | None:
        return dict(self.doc, _id="oid") if self.doc else None

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
        self.channels = _Channels(platform)


class _Yt:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._error = error

    def reschedule_video(self, youtube_video_id: str, publish_at: str) -> str:
        self.calls.append((youtube_video_id, publish_at))
        if self._error:
            raise self._error
        return publish_at


class _Manager:
    def __init__(self, service: _Yt | None) -> None:
        self._service = service

    async def get_service(self, _channel_id: str) -> _Yt | None:
        return self._service


SCHEDULED = {"channel_id": "c1", "video_id": "v1", "status": "scheduled", "youtube_video_id": "yt1"}


def _run(
    doc: dict[str, Any] | None, yt: _Yt | None, new_time: datetime | None, platform: str = "youtube", **kw: Any
) -> tuple[Any, _Db]:
    db = _Db(doc, platform)
    service = VideoService(db=db, youtube_manager=_Manager(yt))  # type: ignore[arg-type]
    return asyncio.run(service.reschedule_video("c1", "v1", new_time, **kw)), db


def test_rescheduling_changes_youtube_first_then_our_record():
    yt = _Yt()
    when = now_ist() + timedelta(days=1)
    result, db = _run(SCHEDULED, yt, when)
    assert yt.calls and yt.calls[0][0] == "yt1" and yt.calls[0][1].endswith("Z")
    assert db.videos.sets[0]["scheduled_at"] == when
    assert result["ok"] and result["youtube_publish_at"] == yt.calls[0][1]


def test_when_youtube_refuses_our_record_is_left_untouched():
    """If we recorded the new time anyway, the app would promise a time YouTube will not keep."""

    class _Refused(Exception):
        reason = "The scheduled publishing time must be in the future"

    db = _Db(SCHEDULED)
    service = VideoService(db=db, youtube_manager=_Manager(_Yt(_refuse(_Refused()))))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be in the future"):
        asyncio.run(service.reschedule_video("c1", "v1", now_ist() + timedelta(days=1)))
    assert db.videos.sets == []


def _refuse(exc: Exception) -> Exception:
    """An HttpError stand-in the service recognises: it only needs the type, not Google's internals."""
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 400
        reason = getattr(exc, "reason", "bad")

    err = HttpError(_Resp(), b"{}")  # type: ignore[arg-type]
    err.reason = _Resp.reason  # type: ignore[misc]
    return err


def test_a_past_time_is_refused_before_youtube_is_called():
    yt = _Yt()
    with pytest.raises(ValueError, match="at least a minute"):
        _run(SCHEDULED, yt, now_ist() - timedelta(hours=1))
    assert yt.calls == []


def test_a_scheduled_video_needs_an_explicit_time():
    with pytest.raises(ValueError, match="Choose the new publish time"):
        _run(SCHEDULED, _Yt(), None)


def test_a_video_never_uploaded_to_youtube_cannot_be_moved_there():
    with pytest.raises(ValueError, match="not been uploaded"):
        _run({**SCHEDULED, "youtube_video_id": None}, _Yt(), now_ist() + timedelta(days=1))


def test_an_instagram_scheduled_video_is_refused_with_a_reason():
    with pytest.raises(ValueError, match="Instagram"):
        _run(SCHEDULED, _Yt(), now_ist() + timedelta(days=1), platform="instagram")


def test_a_channel_with_no_youtube_token_says_to_reconnect():
    with pytest.raises(ChannelNotConnectedError):
        _run(SCHEDULED, None, now_ist() + timedelta(days=1))


def test_a_comment_can_be_changed_in_the_same_request_and_left_alone_otherwise():
    when = now_ist() + timedelta(days=1)
    _, db = _run(SCHEDULED, _Yt(), when, first_comment="new text")
    assert db.videos.sets[0]["first_comment"] == "new text"
    _, db = _run(SCHEDULED, _Yt(), when)
    assert "first_comment" not in db.videos.sets[0]


def test_a_published_video_is_still_refused_as_neither_queued_nor_scheduled():
    with pytest.raises(ValueError, match="queued or scheduled"):
        _run({**SCHEDULED, "status": "published"}, _Yt(), now_ist() + timedelta(days=1))


def test_a_queued_video_still_reschedules_in_our_queue_without_calling_youtube():
    """The original behaviour: nothing has been uploaded yet, so there is nothing on YouTube to change."""
    yt = _Yt()

    class _Queue:
        async def update_one(self, *_a: Any, **_k: Any) -> None:
            return None

    db = _Db({"channel_id": "c1", "video_id": "v1", "status": "queued"})
    db.schedule_queue = _Queue()  # type: ignore[attr-defined]
    service = VideoService(db=db, youtube_manager=_Manager(yt))  # type: ignore[arg-type]
    asyncio.run(service.reschedule_video("c1", "v1", now_ist() + timedelta(days=1)))
    assert yt.calls == [] and db.videos.sets[0]["scheduled_at"] is not None
