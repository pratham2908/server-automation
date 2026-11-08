"""The city-demand tally, ``dm_city_answers``: one row per message or comment that named (or was read for) a city.

Written by the meet DM flow (a city typed in the chat, ``source: dm``) and by city comment rules
(``source: comment``, keyed ``comment:<id>``). ``GET …/instagram/dm-setup/city-answers`` reads it per
channel. A row is only ever inserted, so a redelivered webhook or a second poll changes nothing — except a
comment row's ``like_count``, which the comment poll keeps fresh (``refresh_likes``) so a city vote can
be won by likes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Protocol

import requests
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.instagram import graph_error_code
from app.services.meet_city import CityAnswer

SOURCE_DM = "dm"
SOURCE_COMMENT = "comment"


# Graph's "this object does not exist": the comment was deleted (or its author's account is gone).
GONE_CODE = 100
# Comments this recent keep having their likes read; a vote is over long before.
LIKES_WINDOW = timedelta(days=14)
# How often one comment's likes are read again.
LIKES_EVERY = timedelta(minutes=30)
# Graph's multi-id read takes up to 50 ids, but for comments 50 is refused as "(#2) Service temporarily
# unavailable" every time (40 is served): 25 leaves room, and a refused batch is split (``read_like_counts``).
LIKES_PER_CALL = 25
# Meta's "try again" code, which it also gives a request that asks too much at once.
TRANSIENT_CODE = 2
LIKES_PER_PASS = 500


class LikeReader(Protocol):
    def get_comment_like_counts(self, comment_ids: list[str]) -> dict[str, int]: ...


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


def _is_transient(exc: requests.RequestException) -> bool:
    if graph_error_code(exc) == TRANSIENT_CODE:
        return True
    try:
        return bool(((exc.response.json() if exc.response is not None else {}).get("error") or {}).get("is_transient"))
    except ValueError:
        return False


async def read_like_counts(reader: LikeReader, ids: list[str]) -> tuple[dict[str, int], list[str]]:
    """Likes per comment id, and the ids that could not be read (deleted, or hidden from us).

    A batch Graph refuses as too much at once is read in halves; one it refuses because a comment is gone
    is read a comment at a time, so one deleted comment doesn't hide the rest. Anything else (a rate
    limit, an outage, or a single comment still "unavailable") raises: try again on the next pass.

    A batch can also succeed and simply leave a deleted comment out (asked alone, Graph says it doesn't
    exist), so an id missing from the answer is asked again alone, and counts as gone if it is still missing.
    """
    try:
        found = await asyncio.to_thread(reader.get_comment_like_counts, ids)
    except requests.RequestException as exc:
        if len(ids) > 1 and _is_transient(exc):
            mid = len(ids) // 2
            first, lost_first = await read_like_counts(reader, ids[:mid])
            second, lost_second = await read_like_counts(reader, ids[mid:])
            return {**first, **second}, lost_first + lost_second
        if graph_error_code(exc) != GONE_CODE:
            raise
        if len(ids) == 1:
            return {}, ids
    else:
        missing = [cid for cid in ids if cid not in found]
        if not missing:
            return found, []
        if len(ids) == 1:
            return found, missing
        ids = missing
        likes_so_far = dict(found)
        gone_so_far: list[str] = []
        for cid in ids:
            got, lost = await read_like_counts(reader, [cid])
            likes_so_far.update(got)
            gone_so_far.extend(lost)
        return likes_so_far, gone_so_far
    likes: dict[str, int] = {}
    gone: list[str] = []
    for cid in ids:
        found, lost = await read_like_counts(reader, [cid])
        likes.update(found)
        gone.extend(lost)
    return likes, gone


async def refresh_likes(db: AsyncIOMotorDatabase, reader: LikeReader, channel_id: str, now: datetime) -> int:
    """Read the likes on this channel's recent city comments that are due. Returns how many were read."""
    query: dict[str, Any] = {
        "channel_id": channel_id,
        "source": SOURCE_COMMENT,
        "answered_at": {"$gte": now - LIKES_WINDOW},
        "comment_gone": {"$ne": True},
        "$or": [{"likes_at": {"$exists": False}}, {"likes_at": {"$lte": now - LIKES_EVERY}}],
    }
    rows = await db.dm_city_answers.find(query, {"message_id": 1}).limit(LIKES_PER_PASS).to_list(LIKES_PER_PASS)
    ids = [str(r["message_id"]).removeprefix("comment:") for r in rows]
    read = 0
    for start in range(0, len(ids), LIKES_PER_CALL):
        likes, gone = await read_like_counts(reader, ids[start : start + LIKES_PER_CALL])
        for cid, count in likes.items():
            await db.dm_city_answers.update_one(
                {"message_id": comment_key(cid)}, {"$set": {"like_count": count, "likes_at": now}}
            )
        for cid in gone:
            await db.dm_city_answers.update_one(
                {"message_id": comment_key(cid)}, {"$set": {"comment_gone": True, "likes_at": now}}
            )
        read += len(likes)
    return read
