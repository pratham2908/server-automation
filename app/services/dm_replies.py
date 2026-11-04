"""The replies our buttons, chips, ice breakers and menu items send, kept by content id.

A tap's payload names a reply (``dm_content.reply_payload``); this is where the name is looked up.
Rows are never edited or deleted: an id IS its content, so a DM sent last week with an old version of
a rule's button still finds the reply it was sent with.
"""

from __future__ import annotations

from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import ValidationError

from app.logger import get_logger
from app.services.dm_content import DmMessage, replies_in, reply_id

logger = get_logger(__name__)


async def register(db: AsyncIOMotorDatabase, channel_id: str, replies: list[DmMessage], now: datetime) -> int:
    """Keep each reply (and every reply nested in it) under its id. Returns how many were new."""
    added = 0
    seen: set[str] = set()
    for reply in [r for top in replies for r in (top, *replies_in(top))]:
        rid = reply_id(channel_id, reply)
        if rid in seen:
            continue
        seen.add(rid)
        result = await db.dm_replies.update_one(
            {"reply_id": rid},
            {
                "$setOnInsert": {
                    "reply_id": rid,
                    "channel_id": channel_id,
                    "message": reply.model_dump(mode="json"),
                    "created_at": now,
                }
            },
            upsert=True,
        )
        added += result.upserted_id is not None
    return added


async def register_message(db: AsyncIOMotorDatabase, channel_id: str, message: DmMessage, now: datetime) -> int:
    """Keep every reply a tap on *message* can send."""
    return await register(db, channel_id, replies_in(message), now)


async def find(db: AsyncIOMotorDatabase, channel_id: str, rid: str) -> DmMessage | None:
    row = await db.dm_replies.find_one({"reply_id": rid, "channel_id": channel_id})
    if row is None:
        return None
    try:
        return DmMessage.model_validate(row["message"])
    except ValidationError as exc:
        # Stored by an older build whose limits were looser: say so rather than send something half-built.
        logger.warning("[DmReplies] Stored reply %s no longer validates: %s", rid, exc)
        return None
