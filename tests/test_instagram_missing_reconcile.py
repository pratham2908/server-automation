"""Reels deleted on Instagram become archived on the second sync that misses them.

The sync only ever asked "which of these are new?", so a deleted reel stayed
`published` forever with metrics frozen at the last good sync. The YouTube side
had reconciliation; Instagram never did.

Absence is trusted here in a way it is not on YouTube — `/{ig-user-id}/media` is
the account's own media list, not a derived view like the uploads playlist. What
one fetch still cannot rule out is a page seam swallowing a live reel, so a reel
has to be absent twice before it is archived.
"""

from __future__ import annotations

import pytest

from app.services.video_service import VideoService


class FakeVideos:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query, _projection=None):
        docs = self.docs

        class Cursor:
            def __aiter__(self):
                async def gen():
                    for d in docs:
                        if d.get("channel_id") != query.get("channel_id"):
                            continue
                        if query.get("instagram_media_id") == {"$ne": None} and not d.get("instagram_media_id"):
                            continue
                        ne = (query.get("status") or {}).get("$ne")
                        if ne is not None and d.get("status") == ne:
                            continue
                        yield d

                return gen()

        return Cursor()

    async def update_many(self, query, update):
        ids = (query.get("instagram_media_id") or {}).get("$in", [])
        ne = (query.get("status") or {}).get("$ne")
        for d in self.docs:
            if d.get("instagram_media_id") not in ids:
                continue
            if ne is not None and d.get("status") == ne:
                continue
            d.update(update["$set"])


def _service(docs) -> VideoService:
    svc = VideoService.__new__(VideoService)  # no R2/Gemini/platform wiring needed
    svc.db = type("DB", (), {"videos": FakeVideos(docs)})()
    return svc


def _reel(media_id, **extra):
    return {"channel_id": "ig", "instagram_media_id": media_id, "status": "published", **extra}


@pytest.mark.asyncio
async def test_a_reel_absent_once_is_marked_not_archived():
    docs = [_reel("a"), _reel("b")]
    archived = await _service(docs)._archive_missing_instagram_videos("ig", {"a"})

    assert archived == 0
    b = docs[1]
    assert b["status"] == "published"
    assert b["missing_since"] is not None


@pytest.mark.asyncio
async def test_a_reel_absent_twice_is_archived():
    docs = [_reel("a"), _reel("b", missing_since="2026-08-26T10:00:00")]
    archived = await _service(docs)._archive_missing_instagram_videos("ig", {"a"})

    assert archived == 1
    b = docs[1]
    assert b["status"] == "archived"
    assert b["platform_deleted"] is True
    assert b["archived_at"] is not None


@pytest.mark.asyncio
async def test_a_reel_that_comes_back_loses_its_strike():
    """A page seam is transient — reappearing must reset, or two unlucky syncs
    on different reels would eventually archive a live one."""
    docs = [_reel("a", missing_since="2026-08-26T10:00:00")]
    archived = await _service(docs)._archive_missing_instagram_videos("ig", {"a"})

    assert archived == 0
    assert docs[0]["status"] == "published"
    assert docs[0]["missing_since"] is None


@pytest.mark.asyncio
async def test_an_empty_fetch_archives_nothing():
    """An empty response is indistinguishable from a wiped account; acting on it
    would archive the entire channel."""
    docs = [_reel("a", missing_since="2026-08-26T10:00:00"), _reel("b", missing_since="2026-08-26T10:00:00")]
    archived = await _service(docs)._archive_missing_instagram_videos("ig", set())

    assert archived == 0
    assert all(d["status"] == "published" for d in docs)


@pytest.mark.asyncio
async def test_already_archived_reels_are_left_alone():
    docs = [_reel("a"), _reel("gone", status="archived", missing_since="2026-08-26T10:00:00")]
    archived = await _service(docs)._archive_missing_instagram_videos("ig", {"a"})

    assert archived == 0
    assert docs[1]["status"] == "archived"


@pytest.mark.asyncio
async def test_another_channels_reels_are_untouched():
    docs = [_reel("a"), {"channel_id": "other", "instagram_media_id": "x", "status": "published"}]
    await _service(docs)._archive_missing_instagram_videos("ig", {"a"})

    assert "missing_since" not in docs[1]
