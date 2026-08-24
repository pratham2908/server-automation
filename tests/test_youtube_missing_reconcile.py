"""Flagging a video as gone from YouTube must be slow to accuse and quick to forgive.

Absence from the uploads playlist is not proof of deletion. On officialgeoranking
OTFaRWx_e9c is public and live yet absent from that playlist, so flagging on
absence alone would brand a working video as deleted. Every candidate is
confirmed with a direct lookup first.

Nothing is ever deleted here: a sync that removed records would turn one bad API
response into permanent data loss.
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


def row(youtube_id, missing=None):
    return {
        "channel_id": "ch",
        "video_id": f"local-{youtube_id}",
        "youtube_video_id": youtube_id,
        "platform_missing_since": missing,
    }


@pytest.mark.asyncio
async def test_a_video_absent_from_the_playlist_but_still_live_is_not_flagged():
    """The exact shape of OTFaRWx_e9c — public, live, missing from the playlist."""
    service = build_service([row("live-but-unlisted-in-playlist")])
    flagged = await service._flag_missing_youtube_videos(
        "ch", youtube_resolving({"live-but-unlisted-in-playlist"}), fetched_ids={"other"}
    )
    assert flagged == 0
    assert service.db.videos._docs[0]["platform_missing_since"] is None


@pytest.mark.asyncio
async def test_a_video_youtube_cannot_resolve_is_flagged():
    service = build_service([row("deleted")])
    flagged = await service._flag_missing_youtube_videos("ch", youtube_resolving(set()), fetched_ids={"other"})
    assert flagged == 1
    assert service.db.videos._docs[0]["platform_missing_since"] is not None


@pytest.mark.asyncio
async def test_nothing_is_ever_deleted():
    service = build_service([row("deleted")])
    await service._flag_missing_youtube_videos("ch", youtube_resolving(set()), fetched_ids={"other"})
    assert len(service.db.videos._docs) == 1


@pytest.mark.asyncio
async def test_an_empty_fetch_flags_nothing():
    """An empty response is indistinguishable from a failed one.

    Reconciling on it would flag every video the channel has.
    """
    service = build_service([row("a"), row("b")])
    flagged = await service._flag_missing_youtube_videos("ch", youtube_resolving(set()), fetched_ids=set())
    assert flagged == 0
    assert all(d["platform_missing_since"] is None for d in service.db.videos._docs)


@pytest.mark.asyncio
async def test_a_video_that_comes_back_loses_the_flag():
    """Otherwise one bad response marks a video missing forever."""
    service = build_service([row("returned", missing="2026-08-01")])
    await service._flag_missing_youtube_videos("ch", youtube_resolving(set()), fetched_ids={"returned"})
    assert service.db.videos._docs[0]["platform_missing_since"] is None


@pytest.mark.asyncio
async def test_the_first_sighting_is_kept_across_repeat_syncs():
    """'Missing since' must answer when it vanished, not when we last looked."""
    service = build_service([row("deleted", missing="2026-08-01")])
    flagged = await service._flag_missing_youtube_videos("ch", youtube_resolving(set()), fetched_ids={"other"})
    # The count reports how many are missing now, so it still counts this one —
    # but the timestamp is the original sighting, not this sync.
    assert flagged == 1
    assert service.db.videos._docs[0]["platform_missing_since"] == "2026-08-01"
