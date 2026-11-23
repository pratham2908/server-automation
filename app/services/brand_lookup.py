"""Brand lookups through Instagram's business_discovery: pure parts (handle clean-up, query, shaping, cache).

business_discovery reads another Business or Creator account's public profile and recent media through
our own account, in one Graph call. Personal accounts are refused by Instagram, which is why a lookup can
end as ``not_business``. The Graph call itself lives in ``InstagramService.business_discovery``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any, Literal

MEDIA_LIMIT_MAX = 50
CACHE_TTL_SECONDS = 24 * 3600
MAX_BATCH = 50

_HANDLE = re.compile(r"^[a-z0-9._]{1,30}$")
_URL = re.compile(r"instagram\.com/([^/?#\s]+)", re.IGNORECASE)

Status = Literal["ok", "not_business", "invalid_handle", "error"]

MEDIA_FIELDS = "caption,permalink,timestamp,like_count,comments_count,media_type"
PROFILE_FIELDS = "username,name,biography,website,followers_count,media_count"


def clean_handle(raw: str) -> str | None:
    """A bare lowercase handle from '@Name', 'name' or an instagram.com URL; None if it cannot be one."""
    text = raw.strip()
    url = _URL.search(text)
    if url:
        text = url.group(1)
    text = text.lstrip("@").strip().lower()
    return text if _HANDLE.fullmatch(text) else None


def discovery_fields(handle: str, media_limit: int) -> str:
    """The ``fields`` value that asks for the profile and its latest media in a single call."""
    return f"business_discovery.username({handle}){{{PROFILE_FIELDS},media.limit({media_limit}){{{MEDIA_FIELDS}}}}}"


def shape(handle: str, response: dict[str, Any]) -> dict[str, Any]:
    """The lookup result from Graph's reply. like_count is None when the brand hides likes."""
    node = response.get("business_discovery") or {}
    media = [
        {
            "caption": m.get("caption"),
            "permalink": m.get("permalink"),
            "timestamp": m.get("timestamp"),
            "like_count": m.get("like_count"),
            "comments_count": m.get("comments_count"),
            "media_type": m.get("media_type"),
        }
        for m in (node.get("media") or {}).get("data", [])
    ]
    return {
        "handle": handle,
        "status": "ok",
        "username": node.get("username") or handle,
        "name": node.get("name"),
        "biography": node.get("biography"),
        "website": node.get("website"),
        "followers_count": node.get("followers_count"),
        "media_count": node.get("media_count"),
        "media": media,
        "error": None,
    }


def failure(handle: str, status: Status, message: str) -> dict[str, Any]:
    return {
        "handle": handle,
        "status": status,
        "username": None,
        "name": None,
        "biography": None,
        "website": None,
        "followers_count": None,
        "media_count": None,
        "media": [],
        "error": message,
    }


class LookupCache:
    """Successful lookups kept for a day, so re-scoring a brand costs no Graph call. Failures are not kept."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._clock = clock
        self._ttl = ttl
        self._items: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}

    def get(self, handle: str, media_limit: int) -> dict[str, Any] | None:
        hit = self._items.get((handle, media_limit))
        if hit is None or self._clock() - hit[0] > self._ttl:
            return None
        return hit[1]

    def put(self, handle: str, media_limit: int, result: dict[str, Any]) -> None:
        if result["status"] == "ok":
            self._items[(handle, media_limit)] = (self._clock(), result)
