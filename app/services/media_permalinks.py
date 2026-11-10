"""A media id's permalink (and so its shortcode), read from Instagram once and kept in ``media_permalinks``.

Our ``videos`` hold the Graph media id, while links people see and share carry the shortcode
(``instagram.com/reel/<shortcode>/``); this is the bridge between the two.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Protocol

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger

logger = get_logger(__name__)

_SHORTCODE = re.compile(r"instagram\.com/(?:reels?|p|tv)/([A-Za-z0-9_-]+)")


class PermalinkReader(Protocol):
    def get_permalinks(self, media_ids: list[str]) -> dict[str, str]: ...


def shortcode(link: str | None) -> str | None:
    found = _SHORTCODE.search(link or "")
    return found.group(1) if found else None


async def permalinks(
    db: AsyncIOMotorDatabase, graph: PermalinkReader | None, media_ids: Sequence[str]
) -> dict[str, str]:
    """Permalink per media id: kept ones first, the rest read from Instagram (once) and kept.

    A media id Instagram doesn't return (a deleted post) is left out, and asked again next time.
    """
    ids = sorted({m for m in media_ids if m})
    known = {
        str(d["media_id"]): str(d["permalink"])
        async for d in db.media_permalinks.find({"media_id": {"$in": ids}}, {"media_id": 1, "permalink": 1})
    }
    missing = [m for m in ids if m not in known]
    if missing and graph is not None:
        try:
            found = await asyncio.to_thread(graph.get_permalinks, missing)
        except Exception as exc:  # noqa: BLE001 - callers treat a missing link as "unknown", never as an error
            logger.warning("[Permalinks] Reading permalinks failed: %s", exc)
            found = {}
        for mid, link in found.items():
            await db.media_permalinks.update_one(
                {"media_id": mid}, {"$set": {"media_id": mid, "permalink": link}}, upsert=True
            )
        known.update(found)
    return known
