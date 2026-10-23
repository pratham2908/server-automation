"""A small in-memory stand-in for the Motor collections the Threads code touches.

Covers the query operators it uses ($in, $ne, $lte, $gte, $exists) and the
update operators ($set with dotted keys, $unset, upsert). Not a Mongo engine.
"""

from __future__ import annotations

import copy
import itertools
from typing import Any

_ids = itertools.count(1)


def _get(doc: dict[str, Any], dotted: str) -> Any:
    cur: Any = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


class _Missing:
    pass


_MISSING = _Missing()


def _match_value(value: Any, cond: Any) -> bool:
    if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
        for op, arg in cond.items():
            present = value is not _MISSING
            if op == "$in" and not (present and value in arg):
                return False
            if op == "$ne" and present and value == arg:
                return False
            if op == "$lte" and not (present and value is not None and value <= arg):
                return False
            if op == "$gte" and not (present and value is not None and value >= arg):
                return False
            if op == "$exists" and present != bool(arg):
                return False
        return True
    return value is not _MISSING and value == cond


def matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    return all(_match_value(_get(doc, k), v) for k, v in query.items())


def _set(doc: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = doc
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _unset(doc: dict[str, Any], dotted: str) -> None:
    parts = dotted.split(".")
    cur: Any = doc
    for part in parts[:-1]:
        cur = cur.get(part, {})
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


class Cursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def sort(self, key: str, direction: int = 1) -> Cursor:
        self._docs.sort(key=lambda d: (_get(d, key) is _MISSING, _get(d, key)), reverse=direction < 0)
        return self

    def limit(self, n: int) -> Cursor:
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        return self._docs if length is None else self._docs[:length]

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d

        return gen()


class Result:
    def __init__(self, matched: int = 0, inserted_id: Any = None) -> None:
        self.matched_count = matched
        self.modified_count = matched
        self.inserted_id = inserted_id


class Collection:
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs: list[dict[str, Any]] = [dict(d, _id=d.get("_id", next(_ids))) for d in (docs or [])]

    def find(self, query: dict[str, Any] | None = None, projection: Any = None) -> Cursor:
        return Cursor([copy.deepcopy(d) for d in self.docs if matches(d, query or {})])

    async def find_one(self, query: dict[str, Any] | None = None, projection: Any = None, sort: Any = None):
        found = [d for d in self.docs if matches(d, query or {})]
        return copy.deepcopy(found[0]) if found else None

    async def insert_one(self, doc: dict[str, Any]) -> Result:
        doc.setdefault("_id", next(_ids))
        self.docs.append(copy.deepcopy(doc))
        return Result(inserted_id=doc["_id"])

    async def update_one(self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False) -> Result:
        target = next((d for d in self.docs if matches(d, query)), None)
        if target is None:
            if not upsert:
                return Result(0)
            target = {k: v for k, v in query.items() if not isinstance(v, dict)}
            target["_id"] = next(_ids)
            self.docs.append(target)
        for k, v in (update.get("$set") or {}).items():
            _set(target, k, copy.deepcopy(v))
        for k in update.get("$unset") or {}:
            _unset(target, k)
        return Result(1)

    async def delete_one(self, query: dict[str, Any]) -> Result:
        for i, d in enumerate(self.docs):
            if matches(d, query):
                del self.docs[i]
                return Result(1)
        return Result(0)

    async def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for d in self.docs if matches(d, query))


class DB:
    """Collections spring into existence on first use, like Mongo's."""

    def __init__(self, **collections: list[dict[str, Any]]) -> None:
        self._cols: dict[str, Collection] = {name: Collection(docs) for name, docs in collections.items()}

    def __getattr__(self, name: str) -> Collection:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._cols.setdefault(name, Collection())
