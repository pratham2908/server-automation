"""(channel_id, instagram_media_id) is unique: what each writer does when the index catches a duplicate."""

from __future__ import annotations

from typing import Any

import pytest
from pymongo.errors import DuplicateKeyError

from app.services.auto_publisher import record_published_media
from tests.mongo_fakes import DB

CHANNEL = "zara kapoor"


class _UniqueVideos:
    """Wraps the fake collection with the real index's rule: one record per (channel, media id)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def _taken(self, channel: str, mid: Any, not_video: str | None = None) -> bool:
        return (
            isinstance(mid, str)
            and mid > ""
            and any(
                d.get("channel_id") == channel and d.get("instagram_media_id") == mid and d.get("video_id") != not_video
                for d in self.inner.docs
            )
        )

    async def update_one(self, query: dict[str, Any], update: dict[str, Any], **kw: Any) -> Any:
        mid = (update.get("$set") or {}).get("instagram_media_id")
        if self._taken(query.get("channel_id"), mid, not_video=query.get("video_id")):
            raise DuplicateKeyError("E11000 duplicate key")
        return await self.inner.update_one(query, update, **kw)

    async def insert_one(self, doc: dict[str, Any]) -> Any:
        if self._taken(doc.get("channel_id"), doc.get("instagram_media_id")):
            raise DuplicateKeyError("E11000 duplicate key")
        return await self.inner.insert_one(doc)


def _db(docs: list[dict[str, Any]]) -> DB:
    db = DB(videos=docs)
    db.videos = _UniqueVideos(db.videos)  # type: ignore[attr-defined]
    return db


@pytest.mark.asyncio
async def test_the_publisher_takes_the_media_id_from_a_stray_sync_import() -> None:
    # The sync imported the brand-new reel a moment before the publisher could record it on its own record.
    db = _db(
        [
            {"channel_id": CHANNEL, "video_id": "ours", "status": "scheduled"},
            {"channel_id": CHANNEL, "video_id": "stray", "instagram_media_id": "924", "status": "published"},
        ]
    )
    await record_published_media(db, CHANNEL, "ours", "924", {"instagram_media_id": "924", "status": "published"})
    docs = db.videos.inner.docs
    assert [(d["video_id"], d.get("instagram_media_id"), d["status"]) for d in docs] == [("ours", "924", "published")]


@pytest.mark.asyncio
async def test_the_publisher_records_normally_when_nothing_clashes() -> None:
    db = _db([{"channel_id": CHANNEL, "video_id": "ours", "status": "scheduled"}])
    await record_published_media(db, CHANNEL, "ours", "925", {"instagram_media_id": "925", "status": "published"})
    assert db.videos.inner.docs[0]["instagram_media_id"] == "925"


@pytest.mark.asyncio
async def test_another_channels_reel_with_the_same_id_is_never_touched() -> None:
    db = _db(
        [
            {"channel_id": CHANNEL, "video_id": "ours", "status": "scheduled"},
            {"channel_id": "TryAlgoViz", "video_id": "theirs", "instagram_media_id": "924", "status": "published"},
        ]
    )
    await record_published_media(db, CHANNEL, "ours", "924", {"instagram_media_id": "924", "status": "published"})
    assert {d["video_id"] for d in db.videos.inner.docs} == {"ours", "theirs"}
