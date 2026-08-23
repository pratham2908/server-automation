"""Promotion from 'processing' to 'ready' after a successful analysis.

Imported/batch videos wait in 'processing' until AI packaging writes their
metadata; analysis then promotes them. Re-analysing a live video must never
rewind its status — that guard is what these tests pin down.
"""

import pytest

from app.services.retention_analysis import promote_processing_to_ready


class FakeVideos:
    """Minimal videos collection: update_one / find_one / update_many with the
    subset of query fields the promotion uses."""

    def __init__(self, docs):
        self.docs = docs

    @staticmethod
    def _matches(doc, query):
        return all(doc.get(k) == v for k, v in query.items())

    async def update_one(self, query, update):
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(update["$set"])
                return
        return

    async def update_many(self, query, update):
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(update["$set"])

    async def find_one(self, query, projection=None):
        for doc in self.docs:
            if self._matches(doc, query):
                return doc
        return None


class FakeDB:
    def __init__(self, docs):
        self.videos = FakeVideos(docs)


def _by_id(docs, vid):
    return next(d for d in docs if d["video_id"] == vid)


@pytest.mark.asyncio
async def test_processing_video_becomes_ready():
    docs = [{"channel_id": "c1", "video_id": "v1", "status": "processing"}]
    await promote_processing_to_ready(FakeDB(docs), "c1", "v1")
    assert _by_id(docs, "v1")["status"] == "ready"


@pytest.mark.asyncio
async def test_multi_channel_siblings_are_promoted_together():
    docs = [
        {"channel_id": "c1", "video_id": "v1", "status": "processing", "multi_channel_group_id": "g"},
        {"channel_id": "c2", "video_id": "v2", "status": "processing", "multi_channel_group_id": "g"},
        {"channel_id": "c3", "video_id": "v3", "status": "processing", "multi_channel_group_id": "other"},
    ]
    await promote_processing_to_ready(FakeDB(docs), "c1", "v1")
    assert _by_id(docs, "v1")["status"] == "ready"
    assert _by_id(docs, "v2")["status"] == "ready"  # sibling in the same group
    assert _by_id(docs, "v3")["status"] == "processing"  # unrelated group untouched


@pytest.mark.asyncio
async def test_already_live_video_is_not_rewound():
    # A manual re-analysis ("predict") of a published/ready video must not move it.
    for live in ("ready", "queued", "scheduled", "published"):
        docs = [{"channel_id": "c1", "video_id": "v1", "status": live}]
        await promote_processing_to_ready(FakeDB(docs), "c1", "v1")
        assert _by_id(docs, "v1")["status"] == live


@pytest.mark.asyncio
async def test_a_published_sibling_is_not_rewound():
    docs = [
        {"channel_id": "c1", "video_id": "v1", "status": "processing", "multi_channel_group_id": "g"},
        {"channel_id": "c2", "video_id": "v2", "status": "published", "multi_channel_group_id": "g"},
    ]
    await promote_processing_to_ready(FakeDB(docs), "c1", "v1")
    assert _by_id(docs, "v1")["status"] == "ready"
    assert _by_id(docs, "v2")["status"] == "published"  # guard protects the live sibling
