"""In-memory stand-ins for the posts tests: a small Mongo, R2, Instagram and SMTP.

The Mongo fake stores datetimes the way Mongo returns them — naive UTC — so a
comparison that forgets ``assume_utc`` fails here the way it would in
production. It implements only the operators the posts code uses.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from app.timezone import assume_utc


def _to_mongo(value: Any) -> Any:
    if isinstance(value, datetime):
        return assume_utc(value).astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(value, dict):
        return {k: _to_mongo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_mongo(v) for v in value]
    return value


def _cmp(a: Any) -> Any:
    return assume_utc(a) if isinstance(a, datetime) else a


def _get(doc: dict, dotted: str) -> Any:
    cur: Any = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


_MISSING = object()


def _field_matches(value: Any, cond: Any) -> bool:
    if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
        for op, arg in cond.items():
            present = value is not _MISSING
            v = None if value is _MISSING else value
            if op == "$in" and v not in arg:
                return False
            if op == "$nin" and v in arg:
                return False
            if op == "$ne" and v == arg:
                return False
            if op == "$exists" and present != bool(arg):
                return False
            if op in ("$lte", "$lt", "$gte", "$gt"):
                if v is None:
                    return False
                left, right = _cmp(v), _cmp(arg)
                ok = {
                    "$lte": left <= right,
                    "$lt": left < right,
                    "$gte": left >= right,
                    "$gt": left > right,
                }[op]
                if not ok:
                    return False
        return True
    return (None if value is _MISSING else value) == cond


def matches(doc: dict, query: dict) -> bool:
    for key, cond in query.items():
        if key == "$or":
            if not any(matches(doc, q) for q in cond):
                return False
        elif key == "$nor":
            if any(matches(doc, q) for q in cond):
                return False
        elif not _field_matches(_get(doc, key), cond):
            return False
    return True


def _set_dotted(doc: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = doc
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


class FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    def sort(self, key: str, direction: int = 1) -> FakeCursor:
        self._docs.sort(
            key=lambda d: (d.get(key) is None, _cmp(d.get(key)) if d.get(key) is not None else 0),
            reverse=direction < 0,
        )
        return self

    async def to_list(self, length: int | None = None) -> list[dict]:
        return list(self._docs)

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d

        return gen()


class FakeCollection:
    def __init__(self, docs: list[dict] | None = None):
        self.docs: list[dict] = [_to_mongo(copy.deepcopy(d)) for d in (docs or [])]

    def find(self, query: dict | None = None, projection: dict | None = None) -> FakeCursor:
        return FakeCursor([copy.deepcopy(d) for d in self.docs if matches(d, query or {})])

    async def find_one(self, query: dict | None = None, projection: dict | None = None) -> dict | None:
        for d in self.docs:
            if matches(d, query or {}):
                return copy.deepcopy(d)
        return None

    async def insert_one(self, doc: dict) -> SimpleNamespace:
        self.docs.append(_to_mongo(copy.deepcopy(doc)))
        return SimpleNamespace(inserted_id=len(self.docs))

    async def update_one(self, query: dict, update: dict, upsert: bool = False) -> SimpleNamespace:
        target = next((d for d in self.docs if matches(d, query)), None)
        if target is None:
            if not upsert:
                return SimpleNamespace(matched_count=0, modified_count=0)
            target = {k: v for k, v in query.items() if not k.startswith("$") and not isinstance(v, dict)}
            for k, v in update.get("$setOnInsert", {}).items():
                _set_dotted(target, k, _to_mongo(copy.deepcopy(v)))
            self.docs.append(target)
        for k, v in update.get("$set", {}).items():
            _set_dotted(target, k, _to_mongo(copy.deepcopy(v)))
        for k in update.get("$unset", {}):
            target.pop(k, None)
        for k, v in update.get("$inc", {}).items():
            target[k] = target.get(k, 0) + v
        for k, v in update.get("$push", {}).items():
            target.setdefault(k, []).append(_to_mongo(copy.deepcopy(v)))
        return SimpleNamespace(matched_count=1, modified_count=1)

    async def delete_one(self, query: dict) -> SimpleNamespace:
        for i, d in enumerate(self.docs):
            if matches(d, query):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    def one(self, **eq: Any) -> dict:
        return next(d for d in self.docs if all(d.get(k) == v for k, v in eq.items()))


class FakeDB:
    def __init__(
        self,
        channels: list[dict] | None = None,
        posts: list[dict] | None = None,
        videos: list[dict] | None = None,
        profile_email: str | None = "owner@example.com",
    ):
        self.channels = FakeCollection(channels)
        self.posts = FakeCollection(posts)
        self.videos = FakeCollection(videos)
        self.errors = FakeCollection()
        self.profiles = FakeCollection([{"email": profile_email}] if profile_email else [])


class FakeR2:
    def __init__(self, sizes: dict[str, int] | None = None):
        self.sizes: dict[str, int] = dict(sizes or {})
        self.deleted: list[str] = []
        self.put_calls: list[tuple[str, str]] = []
        self.get_calls: list[tuple[str, int]] = []

    def generate_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        self.get_calls.append((key, expires_in))
        return f"https://r2.test/{key}?exp={expires_in}"

    def generate_presigned_put_url(self, key: str, expires_in: int = 900, content_type: str = "video/mp4") -> str:
        self.put_calls.append((key, content_type))
        return f"https://r2.test/put/{key}"

    def object_size(self, key: str) -> int | None:
        return self.sizes.get(key)

    def delete_video(self, key: str) -> None:
        self.deleted.append(key)
        self.sizes.pop(key, None)


class FakeInstagram:
    """Scriptable Graph API. ``statuses[container_id]`` is a list consumed one read at a time (last one sticks)."""

    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.statuses: dict[str, list[str]] = {}
        self.default_status = "FINISHED"
        self.published: list[str] = []
        self.comments: list[tuple[str, str]] = []
        self.comment_error: Exception | None = None
        self.create_error: Exception | None = None
        self.media: list[dict[str, Any]] = []
        self.media_page_calls = 0
        self._n = 0

    def _new(self, kind: str, **params: Any) -> str:
        if self.create_error is not None:
            raise self.create_error
        self._n += 1
        cid = f"{kind}-{self._n}"
        self.created.append((cid, params))
        return cid

    def create_image_container(self, ig_user_id, image_url, *, caption=None, is_carousel_item=False, alt_text=None):
        return self._new(
            "img", image_url=image_url, caption=caption, is_carousel_item=is_carousel_item, alt_text=alt_text
        )

    def create_carousel_video_item(self, ig_user_id, video_url):
        return self._new("vid", video_url=video_url)

    def create_carousel_container(self, ig_user_id, children, caption):
        return self._new("car", children=list(children), caption=caption)

    def create_story_container(self, ig_user_id, *, image_url=None, video_url=None):
        return self._new("story", image_url=image_url, video_url=video_url)

    def get_container_status(self, container_id):
        queue = self.statuses.get(container_id)
        if queue:
            code = queue.pop(0) if len(queue) > 1 else queue[0]
        else:
            code = self.default_status
        return code, ("bad media" if code == "ERROR" else "")

    def publish_container(self, ig_user_id, container_id):
        self.published.append(container_id)
        return f"media-{container_id}"

    def get_permalink(self, media_id):
        return f"https://instagram.com/p/{media_id}"

    def post_comment(self, media_id, message):
        if self.comment_error is not None:
            raise self.comment_error
        self.comments.append((media_id, message))
        return "comment-1"

    def get_media_page(self, ig_user_id, *, limit=25, after=None):
        self.media_page_calls += 1
        return {"data": list(self.media[:limit]), "next_cursor": None}


class FakeManager:
    def __init__(self, service: Any):
        self.service = service

    async def get_service(self, channel_id: str) -> Any:
        return self.service
