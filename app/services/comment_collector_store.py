"""Where collectors and their answers live.

- ``comment_collectors``: one document per collector (``comment_collectors.Collector``).
- ``collector_answers``: one row per (collector, comment, answer), so a webhook redelivery or a second read
  changes nothing. The row keeps who wrote the comment and on which reel, for the counts, and the comment's
  likes, re-read on a schedule (``comment_collectors.likes_due``).
- ``comment_log`` rows get ``collected.<collector id>`` once read for a collector, so each comment is read
  once per collector and a collector added later still reads the comments it is due.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

import requests
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.services import comment_collectors as cc
from app.services.city_tally import LIKES_PER_CALL, read_like_counts
from app.services.comment_log import author_key
from app.timezone import assume_utc

LIKES_PER_PASS = 500


class TooManyCollectorsError(ValueError):
    pass


class LikeReader(Protocol):
    def get_comment_like_counts(self, comment_ids: list[str]) -> dict[str, int]: ...


def _out(doc: dict[str, Any] | None) -> cc.Collector | None:
    if not doc:
        return None
    doc = {k: v for k, v in doc.items() if k != "_id"}
    return cc.Collector.model_validate(doc)


async def list_collectors(db: AsyncIOMotorDatabase, channel_id: str) -> list[cc.Collector]:
    docs = await db.comment_collectors.find({"channel_id": channel_id}).sort("created_at", 1).to_list(None)
    return [c for c in (_out(d) for d in docs) if c]


async def get_collector(db: AsyncIOMotorDatabase, channel_id: str, collector_id: str) -> cc.Collector | None:
    return _out(await db.comment_collectors.find_one({"channel_id": channel_id, "id": collector_id}))


async def create_collector(
    db: AsyncIOMotorDatabase, channel_id: str, body: cc.CollectorIn, now: datetime, active_from: datetime | None = None
) -> cc.Collector:
    if await db.comment_collectors.count_documents({"channel_id": channel_id}) >= cc.MAX_COLLECTORS:
        raise TooManyCollectorsError(f"a channel can have at most {cc.MAX_COLLECTORS} collectors")
    collector = cc.Collector(
        **body.model_dump(),
        id=uuid.uuid4().hex[:12],
        channel_id=channel_id,
        active_from=active_from or now,
        created_at=now,
        updated_at=now,
    )
    await db.comment_collectors.insert_one(collector.model_dump())
    return collector


async def update_collector(
    db: AsyncIOMotorDatabase, existing: cc.Collector, body: cc.CollectorIn, now: datetime
) -> cc.Collector:
    """Change what the owner set. Answers already counted stay; new comments are read with the new question."""
    fields = {**body.model_dump(), "updated_at": now}
    if body.enabled and not existing.enabled:
        # Switched back on: comments written while it was off are not read now, as for a new collector.
        fields["active_from"] = now
    await db.comment_collectors.update_one({"id": existing.id}, {"$set": fields})
    return cc.Collector.model_validate({**existing.model_dump(), **fields})


async def delete_collector(db: AsyncIOMotorDatabase, collector: cc.Collector) -> int:
    """Remove a collector and every answer it counted. Returns how many answers went."""
    result = await db.collector_answers.delete_many({"collector_id": collector.id})
    await db.comment_collectors.delete_one({"id": collector.id})
    return result.deleted_count


def answer_row(
    collector: cc.Collector, comment: dict[str, Any], answer: str, now: datetime, **extra: Any
) -> dict[str, Any]:
    return {
        "collector_id": collector.id,
        "channel_id": collector.channel_id,
        "comment_id": str(comment["comment_id"]),
        "media_id": str(comment.get("media_id") or ""),
        "author": str(comment.get("author") or author_key(str(comment.get("username") or ""))),
        "username": str(comment.get("username") or ""),
        "text": str(comment.get("text") or ""),
        "answer": answer,
        "answer_key": cc.fold(answer),
        "commented_at": comment.get("commented_at") or now,
        "created_at": now,
        **extra,
    }


async def insert_rows(db: AsyncIOMotorDatabase, rows: Sequence[dict[str, Any]]) -> int:
    """Keep answer rows, skipping any already kept. Returns how many were new."""
    added = 0
    for row in rows:
        key = {"collector_id": row["collector_id"], "comment_id": row["comment_id"], "answer_key": row["answer_key"]}
        if await db.collector_answers.find_one(key, {"_id": 1}):
            continue
        try:
            await db.collector_answers.insert_one(dict(row))
            added += 1
        except DuplicateKeyError:
            continue  # read twice at the same moment; the unique index let one through
    return added


async def record_answers(
    db: AsyncIOMotorDatabase,
    comment: dict[str, Any],
    answers: dict[str, list[str]],
    collectors: Sequence[cc.Collector],
    now: datetime,
) -> int:
    """Keep one comment's answers and mark it read for each collector asked. Returns how many rows were new."""
    by_id = {c.id: c for c in collectors}
    rows = [answer_row(by_id[cid], comment, a, now) for cid, found in answers.items() if cid in by_id for a in found]
    added = await insert_rows(db, rows)
    await mark_read(db, str(comment["comment_id"]), [c.id for c in collectors], now)
    return added


async def mark_read(db: AsyncIOMotorDatabase, comment_id: str, collector_ids: Sequence[str], now: datetime) -> None:
    if collector_ids:
        await db.comment_log.update_one(
            {"comment_id": comment_id}, {"$set": {f"collected.{cid}": now for cid in collector_ids}}
        )


async def unread_comments(
    db: AsyncIOMotorDatabase, channel_id: str, collectors: Sequence[cc.Collector], limit: int
) -> list[dict[str, Any]]:
    """The channel's comments some enabled collector is due to read, oldest first."""
    due = [
        {"commented_at": {"$gte": c.active_from}, f"collected.{c.id}": {"$exists": False}}
        for c in collectors
        if c.enabled
    ]
    if not due:
        return []
    query = {"channel_id": channel_id, "own": {"$ne": True}, "collect_failed": {"$ne": True}, "$or": due}
    return await db.comment_log.find(query).sort("commented_at", 1).limit(limit).to_list(limit)


async def merge_answers(
    db: AsyncIOMotorDatabase, collector: cc.Collector, sources: Sequence[str], into: str, now: datetime
) -> cc.Collector:
    """Count *sources* as *into* from now on, and rewrite the answers already counted under them."""
    target = cc.clean_label(into)
    if not target:
        raise ValueError("merge into what?")
    merges = dict(collector.merges)
    for src in sources:
        key = cc.fold(src)
        if not key or key == cc.fold(target):
            continue
        merges[key] = target
        async for row in db.collector_answers.find({"collector_id": collector.id, "answer_key": key}):
            clash = await db.collector_answers.find_one(
                {"collector_id": collector.id, "comment_id": row["comment_id"], "answer_key": cc.fold(target)}
            )
            if clash:
                # The comment already counts for the target; the merged answer would be a second vote.
                await db.collector_answers.delete_one({"_id": row["_id"]})
            else:
                await db.collector_answers.update_one(
                    {"_id": row["_id"]}, {"$set": {"answer": target, "answer_key": cc.fold(target)}}
                )
    # Anything that was merged into a source now lands on the new target too.
    merges = {k: (target if cc.fold(v) in {cc.fold(s) for s in sources} else v) for k, v in merges.items()}
    await db.comment_collectors.update_one({"id": collector.id}, {"$set": {"merges": merges, "updated_at": now}})
    return cc.Collector.model_validate({**collector.model_dump(), "merges": merges, "updated_at": now})


def _row_query(
    collector_id: str, media_id: str | None, since: datetime | None, until: datetime | None
) -> dict[str, Any]:
    query: dict[str, Any] = {"collector_id": collector_id}
    if media_id:
        query["media_id"] = media_id
    when: dict[str, Any] = {}
    if since:
        when["$gte"] = since
    if until:
        when["$lt"] = until
    if when:
        query["commented_at"] = when
    return query


async def tally_for(
    db: AsyncIOMotorDatabase,
    collector_id: str,
    *,
    media_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    rows = await db.collector_answers.find(
        _row_query(collector_id, media_id, since, until),
        {"answer": 1, "author": 1, "media_id": 1, "comment_id": 1, "like_count": 1, "commented_at": 1},
    ).to_list(None)
    return {
        "entries": cc.tally(rows),
        "answers": len(rows),
        "comments": len({r["comment_id"] for r in rows}),
        "people": len({r.get("author") for r in rows}),
        "reels": sorted({str(r.get("media_id") or "") for r in rows if r.get("media_id")}),
    }


async def answers_for(
    db: AsyncIOMotorDatabase,
    collector_id: str,
    *,
    answer: str | None = None,
    media_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
    sort: str = "recent",
) -> list[dict[str, Any]]:
    """The comments behind the list (or behind one answer): newest first, or most liked first."""
    query = _row_query(collector_id, media_id, since, until)
    if answer:
        query["answer_key"] = cc.fold(answer)
    rows = await db.collector_answers.find(query, {"_id": 0}).to_list(None)
    newest = sorted(rows, key=lambda r: assume_utc(r["commented_at"]), reverse=True)
    if sort == "likes":
        # Stable: equal likes stay newest first; a comment whose likes aren't read yet goes last.
        newest.sort(key=lambda r: -1 if r.get("like_count") is None else int(r["like_count"]), reverse=True)
    return newest[:limit]


async def likes_due(db: AsyncIOMotorDatabase, channel_id: str, now: datetime) -> list[str]:
    """Comment ids on this channel's answers whose likes are due a read, youngest first."""
    rows = await db.collector_answers.find(
        {"channel_id": channel_id, "commented_at": {"$gte": now - cc.LIKES_WINDOW}, "comment_gone": {"$ne": True}},
        {"comment_id": 1, "commented_at": 1, "likes_at": 1},
    ).to_list(None)
    due: dict[str, datetime] = {}
    for r in rows:
        if cc.likes_due(r["commented_at"], r.get("likes_at"), now):
            due[str(r["comment_id"])] = r["commented_at"]
    return sorted(due, key=lambda cid: due[cid], reverse=True)[:LIKES_PER_PASS]


async def refresh_likes(db: AsyncIOMotorDatabase, reader: LikeReader, channel_id: str, now: datetime) -> int:
    """Read the likes that are due on this channel's answers. Returns how many comments were read."""
    ids = await likes_due(db, channel_id, now)
    read = 0
    for start in range(0, len(ids), LIKES_PER_CALL):
        likes, gone = await read_like_counts(reader, ids[start : start + LIKES_PER_CALL])
        for cid, count in likes.items():
            await db.collector_answers.update_many(
                {"comment_id": cid}, {"$set": {"like_count": count, "likes_at": now}}
            )
        for cid in gone:
            await db.collector_answers.update_many(
                {"comment_id": cid}, {"$set": {"comment_gone": True, "likes_at": now}}
            )
        read += len(likes)
    return read


async def channels_with_collectors(db: AsyncIOMotorDatabase) -> list[str]:
    return [str(c) for c in await db.comment_collectors.distinct("channel_id", {"enabled": True})]


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    await db.comment_collectors.create_index("id", unique=True)
    await db.comment_collectors.create_index([("channel_id", 1), ("created_at", 1)])
    await db.collector_answers.create_index([("collector_id", 1), ("comment_id", 1), ("answer_key", 1)], unique=True)
    await db.collector_answers.create_index([("collector_id", 1), ("commented_at", -1)])
    await db.collector_answers.create_index([("channel_id", 1), ("commented_at", -1)])
    await db.collector_answers.create_index("comment_id")


async def import_city_votes(db: AsyncIOMotorDatabase, collector: cc.Collector, now: datetime) -> int:
    """Copy the channel's city votes from comments (``dm_city_answers``) into *collector*, likes included.

    For moving a channel's city vote onto a collector without asking the AI again: each vote becomes an
    answer under its city, and its comment is marked read for the collector. Returns how many rows were new.
    """
    rows = []
    async for r in db.dm_city_answers.find(
        {"channel_id": collector.channel_id, "source": "comment", "outcome": {"$ne": "ignored"}}
    ):
        city = cc.clean_label(r.get("name"))
        comment_id = str(r.get("message_id") or "").removeprefix("comment:")
        if not city or not comment_id:
            continue
        comment = {
            "comment_id": comment_id,
            "media_id": r.get("media_id"),
            "username": r.get("username") or "",
            "author": author_key(str(r.get("username") or ""), str(r.get("peer_id") or "")),
            "text": r.get("text") or "",
            "commented_at": r.get("answered_at") or r.get("created_at") or now,
        }
        extra: dict[str, Any] = {}
        if r.get("like_count") is not None:
            extra["like_count"] = int(r["like_count"])
        if r.get("likes_at"):
            extra["likes_at"] = r["likes_at"]
        if r.get("comment_gone"):
            extra["comment_gone"] = True
        rows.append(answer_row(collector, comment, city, now, **extra))
    added = await insert_rows(db, rows)
    for row in rows:
        await mark_read(db, row["comment_id"], [collector.id], now)
    return added
