from __future__ import annotations

import asyncio
from typing import Any

from app.models.profile import ProfileInDB
from app.routers.channels import list_channels


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        return list(self._docs)

    def __aiter__(self) -> _Cursor:
        self._iter = iter(self._docs)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return dict(next(self._iter))
        except StopIteration:
            raise StopAsyncIteration from None


class _Collection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs

    def find(self, *_args: Any, **_kwargs: Any) -> _Cursor:
        return _Cursor(self.docs)


class _Db:
    def __init__(self, channels: list[dict[str, Any]], params: list[dict[str, Any]]) -> None:
        self.channels = _Collection(channels)
        self.content_params = _Collection(params)


def test_params_are_attached_only_where_the_channel_has_no_embedded_schema() -> None:
    db = _Db(
        channels=[
            {"_id": 1, "channel_id": "a"},
            {"_id": 2, "channel_id": "b", "content_schema": [{"name": "kept"}]},
            {"_id": 3, "channel_id": "c"},
        ],
        params=[{"_id": 9, "channel_id": "a", "name": "topic"}],
    )
    profile = ProfileInDB.model_construct(id="p")
    result = asyncio.run(list_channels(db=db, current_profile=profile))  # type: ignore[arg-type]
    by_id = {c["channel_id"]: c["content_schema"] for c in result}
    assert by_id["a"] == [{"channel_id": "a", "name": "topic"}]
    assert by_id["b"] == [{"name": "kept"}]
    assert by_id["c"] == []
