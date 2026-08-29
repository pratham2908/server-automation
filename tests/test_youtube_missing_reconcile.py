"""Archiving a video as gone from YouTube must be slow to accuse and self-healing.

Absence from the uploads playlist is not proof of deletion. On officialgeoranking
OTFaRWx_e9c is public and live yet absent from that playlist, so acting on
absence alone would brand a working video as gone. Every candidate is confirmed
with a direct lookup first.

Nothing is ever hard-deleted: "gone from the platform" becomes the archived
(soft-deleted) state, and a video that reappears is restored to published by the
metadata refresh — so one bad response self-heals rather than causing loss.
"""

import pytest

from app.services.video_service import VideoService
from tests.conftest import FakeCollection


class FakeVideos(FakeCollection):
    """Adds the update_many and projection-cursor behaviour this path needs."""

    def find(self, query=None, projection=None):
        docs = [d for d in self._docs if self._matches(d, query or {})]

        class Cursor:
            def __aiter__(self):
                async def gen():
                    for d in docs:
                        yield dict(d)

                return gen()

        return Cursor()

    @staticmethod
    def _matches(doc, query):
        for key, cond in query.items():
            value = doc.get(key)
            if isinstance(cond, dict):
                if "$ne" in cond and value == cond["$ne"]:
                    return False
                if "$in" in cond and value not in cond["$in"]:
                    return False
            elif value != cond:
                return False
        return True

    async def update_many(self, query, update):
        n = 0
        for doc in self._docs:
            if self._matches(doc, query):
                doc.update(update["$set"])
                n += 1
        return type("R", (), {"modified_count": n})()


class FakeDB:
    def __init__(self, docs):
        self.videos = FakeVideos(docs)


def build_service(docs):
    service = VideoService.__new__(VideoService)
    service.db = FakeDB(docs)
    return service


def youtube_resolving(alive: set[str]):
    """A YouTube client that resolves only *alive* ids."""

    class Client:
        def videos(self):
            class Videos:
                def list(self, part, id):
                    ids = id.split(",")

                    class Req:
                        def execute(self):
                            return {"items": [{"id": v} for v in ids if v in alive]}

                    return Req()

            return Videos()

    return type("YT", (), {"_youtube": Client()})()


def row(youtube_id, status="published", **over):
    doc = {
        "channel_id": "ch",
        "video_id": f"local-{youtube_id}",
        "youtube_video_id": youtube_id,
        "status": status,
    }
    doc.update(over)
    return doc


@pytest.mark.asyncio
async def test_a_video_absent_from_the_playlist_but_still_live_is_not_archived():
    """The exact shape of OTFaRWx_e9c — public, live, missing from the playlist."""
    service = build_service([row("live-but-unlisted-in-playlist")])
    gone = await service._archive_missing_youtube_videos(
        "ch", youtube_resolving({"live-but-unlisted-in-playlist"}), fetched_ids={"other"}
    )
    assert gone == 0
    assert service.db.videos._docs[0]["status"] == "published"


@pytest.mark.asyncio
async def test_a_video_youtube_cannot_resolve_is_archived():
    service = build_service([row("deleted")])
    gone = await service._archive_missing_youtube_videos("ch", youtube_resolving(set()), fetched_ids={"other"})
    assert gone == 1
    doc = service.db.videos._docs[0]
    assert doc["status"] == "archived"
    assert doc["platform_deleted"] is True
    assert doc["archived_at"] is not None


@pytest.mark.asyncio
async def test_nothing_is_ever_hard_deleted():
    service = build_service([row("deleted")])
    await service._archive_missing_youtube_videos("ch", youtube_resolving(set()), fetched_ids={"other"})
    # Soft delete: the record survives, just moved to archived.
    assert len(service.db.videos._docs) == 1


@pytest.mark.asyncio
async def test_an_empty_fetch_archives_nothing():
    """An empty response is indistinguishable from a failed one.

    Reconciling on it would archive every video the channel has.
    """
    service = build_service([row("a"), row("b")])
    gone = await service._archive_missing_youtube_videos("ch", youtube_resolving(set()), fetched_ids=set())
    assert gone == 0
    assert all(d["status"] != "archived" for d in service.db.videos._docs)


@pytest.mark.asyncio
async def test_a_video_present_in_this_fetch_is_left_alone():
    """A returned video is live, so this pass must not archive it; the metadata
    refresh (not this method) is what restores it to published."""
    service = build_service([row("returned")])
    gone = await service._archive_missing_youtube_videos("ch", youtube_resolving(set()), fetched_ids={"returned"})
    assert gone == 0
    assert service.db.videos._docs[0]["status"] == "published"


@pytest.mark.asyncio
async def test_an_already_archived_video_is_not_reprocessed():
    """Already off the platform on our side — a repeat sync leaves its archived_at
    (the record of when it vanished) untouched."""
    service = build_service([row("deleted", status="archived", archived_at="2026-08-01")])
    gone = await service._archive_missing_youtube_videos("ch", youtube_resolving(set()), fetched_ids={"other"})
    assert gone == 0
    assert service.db.videos._docs[0]["archived_at"] == "2026-08-01"
