"""Changing a video's visibility — the reversible alternative to deleting it.

The trap this guards: ``videos.update`` REPLACES the part it is given, so
sending privacyStatus alone would clear every other mutable status field and
silently reset things like selfDeclaredMadeForKids on a live video.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services.video_service import VideoService
from app.services.youtube import YouTubeService


class _Videos:
    def __init__(self, status: dict[str, Any] | None) -> None:
        self._status = status
        self.listed: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    def list(self, **kwargs):
        self.listed.append(kwargs)
        return ("list", kwargs)

    def update(self, **kwargs):
        self.updated.append(kwargs)
        return ("update", kwargs)


def _service(status: dict[str, Any] | None = None) -> tuple[YouTubeService, _Videos]:
    svc = YouTubeService.__new__(YouTubeService)
    videos = _Videos(status)

    svc._youtube = type("Y", (), {"videos": lambda _self: videos})()  # type: ignore[attr-defined]

    def execute(request):
        kind, kwargs = request
        if kind == "list":
            return {"items": [{"status": status}] if status is not None else []}
        return {"status": kwargs["body"]["status"]}

    svc._execute = execute  # type: ignore[method-assign]
    return svc, videos


def test_unlisting_preserves_every_other_status_field():
    """videos.update replaces the status part, so anything not sent back is
    cleared — this is how a bulk unlist could silently reset made-for-kids."""
    svc, videos = _service(
        {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
            "license": "youtube",
            "publicStatsViewable": True,
        }
    )
    assert svc.set_video_privacy("vid1", "unlisted") == "unlisted"

    sent = videos.updated[0]["body"]["status"]
    assert sent["privacyStatus"] == "unlisted"
    assert sent["selfDeclaredMadeForKids"] is False
    assert sent["embeddable"] is True
    assert sent["license"] == "youtube"
    assert sent["publicStatsViewable"] is True


def test_the_current_status_is_read_before_writing():
    svc, videos = _service({"privacyStatus": "public"})
    svc.set_video_privacy("vid1", "unlisted")
    assert videos.listed[0]["part"] == "status"
    assert videos.listed[0]["id"] == "vid1"
    assert videos.updated[0]["part"] == "status"
    assert videos.updated[0]["body"]["id"] == "vid1"


def test_publish_at_is_dropped_when_leaving_private():
    """publishAt is only valid alongside privacyStatus=private; sending it with
    unlisted is rejected."""
    svc, videos = _service({"privacyStatus": "private", "publishAt": "2026-12-01T00:00:00Z"})
    svc.set_video_privacy("vid1", "unlisted")
    assert "publishAt" not in videos.updated[0]["body"]["status"]


def test_publish_at_is_kept_when_staying_private():
    svc, videos = _service({"privacyStatus": "private", "publishAt": "2026-12-01T00:00:00Z"})
    svc.set_video_privacy("vid1", "private")
    assert videos.updated[0]["body"]["status"]["publishAt"] == "2026-12-01T00:00:00Z"


def test_an_unknown_privacy_value_is_refused_before_any_call():
    svc, videos = _service({"privacyStatus": "public"})
    with pytest.raises(ValueError, match="public, unlisted or private"):
        svc.set_video_privacy("vid1", "hidden")
    assert videos.listed == [] and videos.updated == []


def test_a_video_youtube_does_not_have_is_refused():
    svc, _ = _service(None)
    with pytest.raises(ValueError, match="no video"):
        svc.set_video_privacy("gone", "unlisted")


# --- the service wrapper ------------------------------------------------------


class _Collection:
    def __init__(self, doc: dict[str, Any] | None) -> None:
        self._doc = doc
        self.sets: list[dict[str, Any]] = []

    async def find_one(self, _query, *_a, **_k):
        return dict(self._doc) if self._doc else None

    async def update_one(self, _flt, update, **_kw):
        self.sets.append(update.get("$set", {}))


class _DB:
    def __init__(self, channel, video) -> None:
        self.channels = _Collection(channel)
        self.videos = _Collection(video)


class _YT:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def set_video_privacy(self, video_id, privacy):
        self.calls.append((video_id, privacy))
        return privacy


def _wrapper(channel, video, yt=None):
    class _Manager:
        async def get_service(self, _cid):
            return yt

    return VideoService(db=_DB(channel, video), youtube_manager=_Manager())


def test_the_wrapper_records_the_applied_privacy():
    yt = _YT()
    svc = _wrapper(
        {"channel_id": "c1", "platform": "youtube"},
        {"_id": "x", "video_id": "v1", "youtube_video_id": "yt1"},
        yt,
    )
    result = asyncio.run(svc.set_video_privacy("c1", "v1", "unlisted"))
    assert result["privacy_status"] == "unlisted"
    assert yt.calls == [("yt1", "unlisted")]
    assert svc.db.videos.sets[0]["metadata.youtube_privacy_status"] == "unlisted"


def test_our_own_status_is_not_changed():
    """An unlisted video is still published; what changed is who can find it.
    Rewriting status would misreport it as scheduled or archived."""
    svc = _wrapper(
        {"channel_id": "c1", "platform": "youtube"},
        {"_id": "x", "video_id": "v1", "youtube_video_id": "yt1"},
        _YT(),
    )
    asyncio.run(svc.set_video_privacy("c1", "v1", "unlisted"))
    assert "status" not in svc.db.videos.sets[0]


def test_a_video_not_on_youtube_yet_is_refused():
    svc = _wrapper({"channel_id": "c1", "platform": "youtube"}, {"_id": "x", "video_id": "v1"}, _YT())
    with pytest.raises(ValueError, match="not published on YouTube"):
        asyncio.run(svc.set_video_privacy("c1", "v1", "unlisted"))


def test_an_instagram_channel_is_refused():
    """Instagram has no equivalent visibility switch."""
    svc = _wrapper({"channel_id": "c1", "platform": "instagram"}, {"_id": "x", "video_id": "v1"}, _YT())
    with pytest.raises(ValueError, match="only be changed on YouTube"):
        asyncio.run(svc.set_video_privacy("c1", "v1", "unlisted"))
