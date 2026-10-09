"""The in-app player's link to a video's stored file."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.video_service import PLAYBACK_URL_TTL_SECONDS, VideoService


class _Videos:
    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs

    async def find_one(self, query: dict, projection: dict | None = None) -> dict | None:
        return next((d for d in self.docs if all(d.get(k) == v for k, v in query.items())), None)


class _FakeR2:
    def __init__(self, present: set[str]) -> None:
        self.present = present
        self.signed: list[tuple[str, int, str | None]] = []

    def file_exists(self, key: str) -> bool:
        return key in self.present

    def generate_presigned_url(self, key: str, expires_in: int = 3600, response_content_type: str | None = None) -> str:
        self.signed.append((key, expires_in, response_content_type))
        return f"https://r2.example/{key}?exp={expires_in}"


def _service(docs: list[dict], present: set[str]) -> tuple[VideoService, _FakeR2]:
    svc = VideoService.__new__(VideoService)
    svc.db = SimpleNamespace(videos=_Videos(docs))
    r2 = _FakeR2(present)
    svc.r2 = r2
    return svc, r2


@pytest.mark.asyncio
async def test_an_unpublished_video_gets_a_short_lived_mp4_link():
    svc, r2 = _service([{"channel_id": "c", "video_id": "v", "r2_object_key": "c/v.mp4"}], {"c/v.mp4"})
    url = await svc.playback_url("c", "v")
    assert url == f"https://r2.example/c/v.mp4?exp={PLAYBACK_URL_TTL_SECONDS}"
    # Served as mp4 whatever it was stored as: Safari refuses octet-stream video.
    assert r2.signed == [("c/v.mp4", PLAYBACK_URL_TTL_SECONDS, "video/mp4")]


@pytest.mark.asyncio
async def test_a_purged_file_is_reported_rather_than_handed_out_as_a_dead_link():
    svc, r2 = _service([{"channel_id": "c", "video_id": "v", "r2_object_key": "c/v.mp4"}], set())
    with pytest.raises(FileNotFoundError, match="removed"):
        await svc.playback_url("c", "v")
    assert r2.signed == []


@pytest.mark.asyncio
async def test_a_video_with_no_stored_file():
    svc, _ = _service([{"channel_id": "c", "video_id": "v"}], set())
    with pytest.raises(FileNotFoundError):
        await svc.playback_url("c", "v")


@pytest.mark.asyncio
async def test_an_unknown_video_or_another_channels_video():
    svc, _ = _service([{"channel_id": "other", "video_id": "v", "r2_object_key": "x"}], {"x"})
    with pytest.raises(ValueError):
        await svc.playback_url("c", "v")
