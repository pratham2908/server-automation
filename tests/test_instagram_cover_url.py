"""A custom thumbnail must survive all the way to the published reel.

The uploader can attach their own thumbnail, and the dashboard showed it — but
the Instagram publish path only ever sent ``thumb_offset``, a timestamp into the
video. So the image was stored, displayed, and then quietly replaced by a frame
Instagram picked itself. Meta's own field for this is ``cover_url``: a URL its
servers fetch while the container processes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.services import auto_publisher
from app.services.instagram import InstagramService


def _service():
    svc = InstagramService(access_token="t")
    sent: dict[str, Any] = {}

    def fake_post(_endpoint, params=None):
        sent.update(params or {})
        return {"id": "container-1", "uri": "https://upload"}

    svc._post = fake_post  # type: ignore[method-assign]
    return svc, sent


# --- the container parameters -------------------------------------------------


def test_a_cover_url_is_sent_to_instagram():
    svc, sent = _service()
    svc.create_reel_container("123", "cap", cover_url="https://r2/thumb.jpg")
    assert sent["cover_url"] == "https://r2/thumb.jpg"


def test_a_cover_url_supersedes_the_offset():
    """Instagram ignores thumb_offset once cover_url is set, so sending both
    would only make the request lie about its intent."""
    svc, sent = _service()
    svc.create_reel_container("123", "cap", thumb_offset=1500, cover_url="https://r2/thumb.jpg")
    assert sent["cover_url"] == "https://r2/thumb.jpg"
    assert "thumb_offset" not in sent


def test_the_offset_still_applies_without_a_cover():
    svc, sent = _service()
    svc.create_reel_container("123", "cap", thumb_offset=1500)
    assert sent["thumb_offset"] == "1500"
    assert "cover_url" not in sent


def test_the_url_publish_path_sends_the_cover_too():
    """publish_reel_from_url is the path the auto-publisher actually uses."""
    svc, sent = _service()
    svc.get_container_status = lambda _cid: ("FINISHED", None)  # type: ignore[method-assign]
    svc.publish_container = lambda _uid, _cid: "media-1"  # type: ignore[method-assign]

    svc.publish_reel_from_url("123", "https://r2/video.mp4", "cap", thumb_offset=1500, cover_url="https://r2/t.jpg")

    assert sent["cover_url"] == "https://r2/t.jpg"
    assert "thumb_offset" not in sent


# --- the auto-publisher -------------------------------------------------------


class _Collection:
    def __init__(self) -> None:
        self.updates: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def update_one(self, flt, update, **_kwargs):
        self.updates.append((flt, update))

    async def delete_one(self, _flt):
        return None

    async def insert_one(self, _doc):
        return None

    async def find_one(self, _flt, *_a, **_k):
        return None


class _FakeDB:
    def __init__(self) -> None:
        self._cols: dict[str, _Collection] = {}

    def __getattr__(self, name: str) -> _Collection:
        return self._cols.setdefault(name, _Collection())


class _FakeR2:
    def __init__(self) -> None:
        self.signed: list[str] = []

    def file_exists(self, _key: str) -> bool:
        return True

    def generate_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        self.signed.append(key)
        return f"https://r2.example/{key}?exp={expires_in}"


class _FakeInstagram:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def publish_reel_from_url(self, **kwargs: Any) -> str:
        self.kwargs = kwargs
        return "media-1"


def _publish(video_doc: dict[str, Any]) -> tuple[_FakeInstagram, _FakeR2]:
    ig, r2 = _FakeInstagram(), _FakeR2()
    ok = asyncio.run(
        auto_publisher._publish_one_reel(
            db=_FakeDB(),
            r2_service=r2,
            instagram_service=ig,
            channel_doc={"channel_id": "c1", "instagram_user_id": "ig1"},
            video_doc={"video_id": "v1", "r2_object_key": "c1/v1.mp4", "title": "t", **video_doc},
            queue_entry={"_id": "q1"},
        )
    )
    assert ok
    return ig, r2


def test_a_custom_thumbnail_is_published_as_the_cover():
    ig, _ = _publish({"custom_thumbnail": True, "thumbnail_r2_key": "c1/thumbnails/v1-custom.jpg"})
    assert ig.kwargs["cover_url"] == "https://r2.example/c1/thumbnails/v1-custom.jpg?exp=3600"


def test_the_cover_url_is_minted_fresh_at_publish_time():
    """The URL stored on the video is presigned at upload; a reel scheduled weeks
    out would otherwise hand Meta a dead link."""
    _, r2 = _publish(
        {
            "custom_thumbnail": True,
            "thumbnail_r2_key": "c1/thumbnails/v1-custom.jpg",
            "thumbnail_url": "https://r2.example/stale?exp=604800",
        }
    )
    assert "c1/thumbnails/v1-custom.jpg" in r2.signed


def test_an_ai_thumbnail_still_uses_the_offset():
    ig, _ = _publish(
        {
            "thumbnail_r2_key": "c1/thumbnails/v1.jpg",
            "ai_packaging": {"best_thumbnail_timestamp": 1.5},
        }
    )
    assert ig.kwargs["cover_url"] is None
    assert ig.kwargs["thumb_offset"] == 1500


def test_a_custom_flag_without_a_stored_key_falls_back_rather_than_crashing():
    ig, _ = _publish({"custom_thumbnail": True, "ai_packaging": {"best_thumbnail_timestamp": 2}})
    assert ig.kwargs["cover_url"] is None
    assert ig.kwargs["thumb_offset"] == 2000
