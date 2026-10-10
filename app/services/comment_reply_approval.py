"""Approve or reject a drafted comment reply.

The thin edge around ``comment_reply_review``: it moves one ``comment_replies``
row out of the queue, posting to the platform on approval. The poster is passed
in so this stays testable without a live YouTube or Instagram client.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.comment_reply_review import (
    STATUS_PENDING,
    STATUS_POSTING,
    STATUS_REJECTED,
    STATUS_REPLIED,
    validate_reply,
)
from app.timezone import now_ist

logger = get_logger(__name__)


class PendingReplyNotFoundError(Exception):
    """No draft is waiting for this comment: never drafted, already sent, or already rejected."""


class ReplyPostFailedError(Exception):
    """The platform refused the reply. The draft is back in the queue so it can be retried."""


async def approve_pending_reply(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    comment_id: str,
    post_reply: Callable[[str, str], str],
    reply_text: str | None = None,
) -> dict[str, Any]:
    """Post the draft (optionally edited) and record it as replied.

    ``post_reply(comment_id, text)`` is blocking; it runs off the event loop.
    """
    query = {"channel_id": channel_id, "comment_id": comment_id}
    existing = await db.comment_replies.find_one({**query, "status": STATUS_PENDING})
    if not existing:
        raise PendingReplyNotFoundError(comment_id)

    final_text = validate_reply(
        reply_text if reply_text is not None else existing.get("reply_text"), existing["platform"]
    )

    # The status flip is the lock: a second approve (double click, two tabs) finds nothing
    # in STATUS_PENDING and gets a not-found instead of posting the reply twice.
    claimed = await db.comment_replies.find_one_and_update(
        {**query, "status": STATUS_PENDING}, {"$set": {"status": STATUS_POSTING}}
    )
    if not claimed:
        raise PendingReplyNotFoundError(comment_id)

    try:
        reply_id = await asyncio.to_thread(post_reply, comment_id, final_text)
    except Exception as exc:
        logger.warning("Approved reply to %s failed on the platform: %s", comment_id, exc)
        await db.comment_replies.update_one(
            query, {"$set": {"status": STATUS_PENDING, "last_error": str(exc)[:500], "reply_text": final_text}}
        )
        raise ReplyPostFailedError(str(exc)) from exc

    done = {
        "status": STATUS_REPLIED,
        "reply_text": final_text,
        "reply_id": reply_id,
        "replied_at": now_ist(),
        "approved": True,
        "edited": final_text != existing.get("suggested_reply"),
    }
    await db.comment_replies.update_one(query, {"$set": done, "$unset": {"last_error": ""}})
    return {**{k: v for k, v in existing.items() if k != "_id"}, **done}


async def reject_pending_reply(db: AsyncIOMotorDatabase, channel_id: str, comment_id: str) -> None:
    """Drop a draft without sending. The row stays, so the cycle never drafts that comment again."""
    result = await db.comment_replies.update_one(
        {"channel_id": channel_id, "comment_id": comment_id, "status": STATUS_PENDING},
        {"$set": {"status": STATUS_REJECTED, "replied_at": now_ist()}},
    )
    if result.matched_count == 0:
        raise PendingReplyNotFoundError(comment_id)
