"""Competitor uniqueness: one YouTube channel id, or one Instagram username, per channel.

The old index was unique on (channel_id, youtube_channel_id) for every document. Instagram competitors
have no YouTube id, so they all stored ``null`` there and a channel could only ever have one.
"""

import asyncio

from app.database import LEGACY_COMPETITOR_INDEX, ensure_competitor_indexes


class RecordingCollection:
    def __init__(self, existing: dict[str, dict]):
        self.indexes = dict(existing)
        self.dropped: list[str] = []
        self.created: list[tuple[list, dict]] = []

    async def index_information(self) -> dict[str, dict]:
        return dict(self.indexes)

    async def drop_index(self, name: str) -> None:
        self.dropped.append(name)
        self.indexes.pop(name)

    async def create_index(self, keys, **kwargs) -> str:
        self.created.append((keys, kwargs))
        self.indexes[kwargs["name"]] = {"key": keys, **kwargs}
        return kwargs["name"]


def _run(coll: RecordingCollection) -> None:
    asyncio.run(ensure_competitor_indexes(coll))


def test_drops_the_index_that_blocked_a_second_instagram_competitor():
    coll = RecordingCollection(
        {
            "_id_": {"key": [("_id", 1)]},
            LEGACY_COMPETITOR_INDEX: {"key": [("channel_id", 1), ("youtube_channel_id", 1)], "unique": True},
        }
    )
    _run(coll)
    assert coll.dropped == [LEGACY_COMPETITOR_INDEX]


def test_each_platform_is_unique_only_where_its_id_is_set():
    coll = RecordingCollection({"_id_": {"key": [("_id", 1)]}})
    _run(coll)
    by_field = {keys[1][0]: opts for keys, opts in coll.created}
    assert by_field["youtube_channel_id"]["unique"] is True
    assert by_field["youtube_channel_id"]["partialFilterExpression"] == {"youtube_channel_id": {"$type": "string"}}
    assert by_field["instagram_username"]["unique"] is True
    assert by_field["instagram_username"]["partialFilterExpression"] == {"instagram_username": {"$type": "string"}}


def test_running_again_changes_nothing_it_already_did():
    coll = RecordingCollection({"_id_": {"key": [("_id", 1)]}})
    _run(coll)
    created_first = len(coll.created)
    _run(coll)
    assert coll.dropped == []
    # create_index is idempotent in Mongo; what matters is the legacy index is never recreated.
    assert LEGACY_COMPETITOR_INDEX not in coll.indexes
    assert len(coll.created) == 2 * created_first
