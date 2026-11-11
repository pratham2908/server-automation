"""Telling the people who asked for a city that its meet reel is out: finding them, and the replies.

Collections:
- ``city_requester_jobs``: one per meet reel seen go up, with its city and how the run went.
- ``city_requester_replies``: one per (channel, city, person), ever. A row is claimed before its reply
  is posted and keeps the id Instagram returns, so a person is told about a city once.

Who asked comes from the channel's "City requests" collector (which reads cities, aliases included, in
every comment) plus any comment that names the city outright, from the comment log. Comments on the new
reel itself, and on a reel about that same city (where naming it isn't a request), don't count.

Before each reply the comment's thread is read: if we already answered it naming the city (by hand, or
the voted-reel thank-you), the person is marked told and skipped. The same read settles a reply whose
outcome we couldn't see (a timeout), so a retry never posts twice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

import requests
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.config import Settings
from app.logger import get_logger
from app.services import city_requesters as cr
from app.services import comment_collectors as cc
from app.services.email_service import resolve_owner_email, send_email
from app.services.errors import get_error_service
from app.services.instagram import graph_error_code, graph_error_message
from app.services.voted_city import city_key
from app.services.voted_city_store import NotFoundError, vote_collector
from app.timezone import assume_utc

logger = get_logger(__name__)

SETTING = "city_requester_replies"  # on the channel: {enabled, since}
SPACING_SECONDS = 8  # between replies; slower is fine, faster looks like a burst
LOOKBACK = timedelta(days=60)
GONE_CODE = 100
_FEATURE = "City requester replies"

JOB_RUNNING, JOB_DONE, JOB_STOPPED = "running", "done", "stopped"
SENT, TOLD, GONE, CLAIMED, FAILED = "sent", "already_told", "comment_gone", "claimed", "failed"


class Graph(Protocol):
    def get_media_page(self, ig_user_id: str, *, limit: int = 25, after: str | None = None) -> dict[str, Any]: ...
    def get_thread(self, comment_id: str, media_id: str = "") -> dict[str, Any]: ...
    def reply_to_comment(self, comment_id: str, message: str) -> str: ...


# ---- Settings -------------------------------------------------------------------------------------------


async def setting(db: AsyncIOMotorDatabase, channel_id: str) -> dict[str, Any]:
    doc = await db.channels.find_one({"channel_id": channel_id}, {SETTING: 1}) or {}
    value = doc.get(SETTING) or {}
    return {"enabled": bool(value.get("enabled")), "since": value.get("since")}


async def set_enabled(db: AsyncIOMotorDatabase, channel_id: str, enabled: bool, now: datetime) -> dict[str, Any]:
    """Switch on (for meet reels going up from now on) or off. Switching on again keeps the first start."""
    current = await setting(db, channel_id)
    since = current["since"] if current["since"] and enabled else now
    await db.channels.update_one(
        {"channel_id": channel_id}, {"$set": {SETTING: {"enabled": enabled, "since": since, "updated_at": now}}}
    )
    return {"enabled": enabled, "since": since}


async def channels_on(db: AsyncIOMotorDatabase) -> list[str]:
    return [str(c["channel_id"]) async for c in db.channels.find({f"{SETTING}.enabled": True}, {"channel_id": 1})]


# ---- Who asked ------------------------------------------------------------------------------------------


async def _keys(db: AsyncIOMotorDatabase, channel_id: str, city: str) -> tuple[str, str, list[str], str | None]:
    """(key, label, every key that means this city, collector id) through the city collector's merges."""
    try:
        collector: cc.Collector | None = await vote_collector(db, channel_id)
    except NotFoundError:
        collector = None
    merges = collector.merges if collector else {}
    key, label = city_key(city, merges)
    aliases = [alias for alias, target in merges.items() if cc.fold(target) == key]
    return key, label, [key, *aliases], collector.id if collector else None


async def requesters(
    db: AsyncIOMotorDatabase, channel_id: str, city: str, reel_media_id: str, now: datetime
) -> tuple[str, str, list[dict[str, Any]]]:
    """(city key, label, one comment per person who asked for the city), oldest asker first."""
    key, label, keys, collector_id = await _keys(db, channel_id, city)
    since = now - LOOKBACK
    rows: list[dict[str, Any]] = []
    if collector_id:
        async for row in db.collector_answers.find(
            {"collector_id": collector_id, "comment_gone": {"$ne": True}, "commented_at": {"$gte": since}}
        ):
            if cc.fold(str(row.get("answer") or "")) in keys:
                rows.append(dict(row))
    # Reels about this city: naming it in their comments is talking about the reel, not asking for it.
    about_city = {
        str(v["instagram_media_id"])
        async for v in db.videos.find(
            {"channel_id": channel_id, "instagram_media_id": {"$nin": [None, ""]}},
            {"instagram_media_id": 1, "title": 1},
        )
        if cr.mentions(str(v.get("title") or ""), keys)
    }
    seen = {str(r["comment_id"]) for r in rows}
    async for c in db.comment_log.find(
        {"channel_id": channel_id, "own": {"$ne": True}, "comment_gone": {"$ne": True}, "commented_at": {"$gte": since}}
    ):
        cid = str(c.get("comment_id"))
        if cid in seen or str(c.get("media_id")) in about_city or not cr.mentions(c.get("text"), keys):
            continue
        rows.append(dict(c))
    rows = [r for r in rows if str(r.get("media_id") or "") != reel_media_id]
    channel = await db.channels.find_one({"channel_id": channel_id}, {"instagram_username": 1}) or {}
    return key, label, cr.one_per_person(rows, [str(channel.get("instagram_username") or "")])


# ---- Jobs -----------------------------------------------------------------------------------------------


async def detect(db: AsyncIOMotorDatabase, graph: Graph, channel_id: str, ig_user_id: str, now: datetime) -> int:
    """Start a job for each meet reel that went up since the feature was switched on. Returns how many."""
    on = await setting(db, channel_id)
    if not on["enabled"] or on["since"] is None:
        return 0
    page = await asyncio.to_thread(graph.get_media_page, ig_user_id, limit=25)
    started = 0
    for item in page.get("data", []):
        city = cr.headline_city(item.get("caption"))
        posted = _media_time(item)
        if city is None or posted is None or posted < _aware(on["since"]):
            continue
        if await db.city_requester_jobs.find_one({"media_id": str(item["id"])}, {"_id": 1}):
            continue
        key, label, _, _ = await _keys(db, channel_id, city)
        try:
            await db.city_requester_jobs.insert_one(
                {
                    "media_id": str(item["id"]),
                    "channel_id": channel_id,
                    "city": label,
                    "city_key": key,
                    "posted_at": posted,
                    "status": JOB_RUNNING,
                    "sent": 0,
                    "error": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        except DuplicateKeyError:
            continue
        started += 1
        logger.info("[CityRequesters] %s's meet is up (%s): telling the people who asked", label, item["id"])
    return started


def _aware(value: Any) -> datetime:
    return assume_utc(value)


def _media_time(item: dict[str, Any]) -> datetime | None:
    try:
        return datetime.strptime(str(item.get("timestamp")), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


async def _row(db: AsyncIOMotorDatabase, job: dict[str, Any], who: str) -> dict[str, Any] | None:
    return await db.city_requester_replies.find_one(
        {"channel_id": job["channel_id"], "city_key": job["city_key"], "username": who}
    )


async def _record(
    db: AsyncIOMotorDatabase, job: dict[str, Any], person: dict[str, Any], fields: dict[str, Any]
) -> None:
    await db.city_requester_replies.update_one(
        {"channel_id": job["channel_id"], "city_key": job["city_key"], "username": person["username"]},
        {
            "$set": {
                "comment_id": str(person["comment_id"]),
                "comment_text": str(person.get("text") or "")[:300],
                "comment_media_id": str(person.get("media_id") or ""),
                "job_media_id": job["media_id"],
                "city": job["city"],
                **fields,
            }
        },
        upsert=True,
    )


async def run_job(
    db: AsyncIOMotorDatabase,
    graph: Graph,
    job: dict[str, Any],
    own: str,
    now_fn: Any,
    sleep: Any = asyncio.sleep,
) -> dict[str, Any]:
    """Tell everyone who asked for the job's city, one reply every few seconds. Stops at the first error.

    Returns the job as stored afterwards.
    """
    key, label, people = await requesters(db, job["channel_id"], job["city"], job["media_id"], now_fn())
    keys = (await _keys(db, job["channel_id"], job["city"]))[2]
    sent = int(job.get("sent") or 0)
    error: str | None = None
    for person in people:
        who = person["username"]
        held = await _row(db, job, who)
        if held and held.get("status") in (SENT, TOLD, GONE):
            continue
        try:
            thread = await asyncio.to_thread(graph.get_thread, str(person["comment_id"]))
        except requests.RequestException as exc:
            if graph_error_code(exc) == GONE_CODE:
                await _record(db, job, person, {"status": GONE, "at": now_fn()})
                continue
            error = f"reading @{who}'s comment: {graph_error_message(exc)}"
            break
        told = cr.already_told(thread.get("replies") or [], own, keys)
        if told:
            await _record(db, job, person, {"status": TOLD, "reply_id": told, "at": now_fn()})
            continue
        text = cr.reply_text(sent, label)
        await _record(db, job, person, {"status": CLAIMED, "text": text, "at": now_fn()})
        try:
            reply_id = await asyncio.to_thread(graph.reply_to_comment, str(person["comment_id"]), text)
        except requests.RequestException as exc:
            if graph_error_code(exc) == GONE_CODE:
                await _record(db, job, person, {"status": GONE, "at": now_fn()})
                continue
            # Left "claimed": the next run reads the thread first, so a reply that did go out isn't repeated.
            error = f"replying to @{who}: {graph_error_message(exc)}"
            await _record(db, job, person, {"status": FAILED, "error": error, "at": now_fn()})
            break
        sent += 1
        await _record(db, job, person, {"status": SENT, "reply_id": reply_id, "error": None, "at": now_fn()})
        await db.city_requester_jobs.update_one({"media_id": job["media_id"]}, {"$set": {"sent": sent}})
        logger.info("[CityRequesters] Told @%s that %s is up (%s)", who, label, reply_id)
        await sleep(SPACING_SECONDS)
    status = JOB_STOPPED if error else JOB_DONE
    await db.city_requester_jobs.update_one(
        {"media_id": job["media_id"]},
        {"$set": {"status": status, "sent": sent, "error": error, "asked": len(people), "updated_at": now_fn()}},
    )
    return dict(await db.city_requester_jobs.find_one({"media_id": job["media_id"]}, {"_id": 0}) or {})


async def report_stop(db: AsyncIOMotorDatabase, settings: Settings, job: dict[str, Any]) -> None:
    """Tell the owner a run stopped, once per stop."""
    if job.get("status") != JOB_STOPPED:
        return
    await get_error_service(db).log_error(
        feature=_FEATURE,
        message=f"Telling {job['city']} requesters stopped: {job.get('error')}",
        context={"media_id": job["media_id"], "channel_id": job["channel_id"], "sent": job.get("sent")},
    )
    body = (
        f"Replies telling people {job['city']} is up stopped after {job.get('sent', 0)} of {job.get('asked', '?')}.\n"
        f"Instagram said: {job.get('error')}\n\n"
        f"Nothing more goes out until it's retried: POST /api/v1/channels/{job['channel_id']}/city-requesters/"
        f"jobs/{job['media_id']}/retry"
    )
    await send_email(settings, await resolve_owner_email(db, settings), f"City replies stopped: {job['city']}", body)


async def retry(db: AsyncIOMotorDatabase, channel_id: str, media_id: str, now: datetime) -> dict[str, Any]:
    result = await db.city_requester_jobs.update_one(
        {"channel_id": channel_id, "media_id": media_id, "status": JOB_STOPPED},
        {"$set": {"status": JOB_RUNNING, "error": None, "updated_at": now}},
    )
    if not result.matched_count:
        raise NotFoundError("no stopped run for that reel")
    return dict(await db.city_requester_jobs.find_one({"media_id": media_id}, {"_id": 0}) or {})


async def jobs(db: AsyncIOMotorDatabase, channel_id: str) -> list[dict[str, Any]]:
    return await db.city_requester_jobs.find({"channel_id": channel_id}, {"_id": 0}).sort("created_at", -1).to_list(100)


async def replies_for(db: AsyncIOMotorDatabase, channel_id: str, city: str) -> list[dict[str, Any]]:
    key = (await _keys(db, channel_id, city))[0]
    return await db.city_requester_replies.find({"channel_id": channel_id, "city_key": key}, {"_id": 0}).to_list(None)


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    await db.city_requester_jobs.create_index("media_id", unique=True)
    await db.city_requester_jobs.create_index([("status", 1), ("created_at", 1)])
    await db.city_requester_replies.create_index([("channel_id", 1), ("city_key", 1), ("username", 1)], unique=True)


def preview(people: Sequence[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    """What a run would send, for the dry-run route."""
    return [
        {
            "username": p["username"],
            "comment_id": str(p["comment_id"]),
            "comment": str(p.get("text") or "")[:200],
            "likes": int(p.get("like_count") or 0),
            "reply": cr.reply_text(i, label),
        }
        for i, p in enumerate(people)
    ]
