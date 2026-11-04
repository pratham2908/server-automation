"""Who an Instagram AI reply must never go to: the same person over and over, or a creep.

Measured on Zara (owner, 2026-11-04): over 10 days, 10 commenters took 52% of her 240 auto-replies, one
of them 23, and most of those were sexual or "come to my DMs" remarks the sentiment model had called
positive — so the bot flirted back. Two rules answer it, for every Instagram channel with AI replies,
on both reply paths (the webhook worker and the polling cycle):

- **A per-person cap**: at most ``CAP_DAY`` replies to one commenter in any rolling 24 hours and
  ``CAP_WEEK`` in any rolling 7 days, counted from ``comment_replies`` rows with status ``replied``.
  A commenter is their Instagram user id when we have it, else their username; past rows that carry
  only a username still count, because the query matches either.
- **A ``creepy`` class** from the classifier is never answered. If the model also marks it explicit,
  the comment is hidden (only its author still sees it) and the hide is logged on the row.

Pure decisions here; ``replied_times`` and ``hide`` are the two thin edges.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

import requests
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.instagram import graph_error_message
from app.timezone import assume_utc

logger = get_logger(__name__)

CAP_DAY = 2
CAP_WEEK = 7
DAY = timedelta(hours=24)
WEEK = timedelta(days=7)

CREEPY = "creepy"
STATUS_SKIPPED_LIMIT = "skipped_limit"
STATUS_SKIPPED_CREEPY = "skipped_creepy"
SKIP_LIMIT = "reply limit for this person"
SKIP_CREEPY = "creepy comment"


def over_cap(replied_at: Iterable[datetime], now: datetime) -> bool:
    """Whether one more reply would break the day or the week cap."""
    times = [assume_utc(t) for t in replied_at]
    in_day = sum(1 for t in times if now - t < DAY)
    in_week = sum(1 for t in times if now - t < WEEK)
    return in_day >= CAP_DAY or in_week >= CAP_WEEK


def person_query(author_id: str, username: str) -> dict[str, Any] | None:
    """How to find a commenter's past replies: by id or by username, whichever we have."""
    keys: list[dict[str, Any]] = []
    if author_id:
        keys.append({"comment_author_id": author_id})
    if username:
        keys.append({"comment_author": username})
    if not keys:
        return None
    return keys[0] if len(keys) == 1 else {"$or": keys}


async def replied_times(
    db: AsyncIOMotorDatabase, channel_id: str, author_id: str, username: str, now: datetime
) -> list[datetime]:
    """When this channel replied to this commenter in the last week."""
    who = person_query(author_id, username)
    if who is None:
        return []
    rows = await db.comment_replies.find(
        {"channel_id": channel_id, "status": "replied", "replied_at": {"$gte": now - WEEK}, **who},
        {"replied_at": 1},
    ).to_list(None)
    return [r["replied_at"] for r in rows if isinstance(r.get("replied_at"), datetime)]


async def at_cap(db: AsyncIOMotorDatabase, channel_id: str, author_id: str, username: str, now: datetime) -> bool:
    return over_cap(await replied_times(db, channel_id, author_id, username, now), now)


async def hide(service: Any, comment_id: str) -> dict[str, Any]:
    """Hide an explicit comment. Returns the fields to log on its row; never raises — the skip stands either way."""
    try:
        await asyncio.to_thread(service.hide_comment, comment_id)
    except requests.RequestException as exc:
        error = graph_error_message(exc)
        logger.warning("[AiReply] Hiding explicit comment %s failed: %s", comment_id, error)
        return {"hidden": False, "hide_error": error[:300]}
    logger.info("[AiReply] Hid explicit comment %s", comment_id)
    return {"hidden": True}
