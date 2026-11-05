"""The city-demand tally, ``dm_city_answers``: one row per message or comment that named (or was read for) a city.

Written by the meet DM flow (a city typed in the chat, ``source: dm``) and by city comment rules
(``source: comment``, keyed ``comment:<id>``). ``GET …/instagram/dm-setup/city-answers`` reads it per
channel. A row is only ever inserted, so a redelivered webhook or a second poll changes nothing.
"""

from __future__ import annotations

from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.meet_city import CityAnswer

SOURCE_DM = "dm"
SOURCE_COMMENT = "comment"


def comment_key(comment_id: str) -> str:
    return f"comment:{comment_id}"


async def record_comment(
    db: AsyncIOMotorDatabase,
    *,
    channel_id: str,
    comment_id: str,
    media_id: str,
    peer_id: str,
    username: str,
    text: str,
    answer: CityAnswer,
    dm_queued: bool,
    commented_at: datetime | None,
    now: datetime,
    place: str | None = None,
) -> bool:
    """Keep what one comment asked for. Returns whether it was new."""
    result = await db.dm_city_answers.update_one(
        {"message_id": comment_key(comment_id)},
        {
            "$setOnInsert": {
                "message_id": comment_key(comment_id),
                "channel_id": channel_id,
                "source": SOURCE_COMMENT,
                "media_id": media_id,
                "peer_id": peer_id,
                "username": username,
                "text": text,
                "name": answer.name,
                # The place as they named it, when that is not the city it is tallied under ("Battersea").
                "place": place or answer.name,
                "normalised": answer.normalised,
                "slug": answer.slug,
                "outcome": answer.outcome,
                "replied": dm_queued,
                "reply_kind": "comment",
                "answered_at": commented_at or now,
                "created_at": now,
            }
        },
        upsert=True,
    )
    return result.upserted_id is not None
