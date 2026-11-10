"""Voted-city reels: the live vote, the owner's posting kit, and the follow-up once the reel is live.

Collections:
- ``voted_cities``: cities already posted as voted reels, left out of the vote. One per (channel, city).
- ``voted_reel_kits``: one per voted reel. The mp4 (in R2), the caption, the deep link to the top comment,
  and, once the reel is live, each follow-up step with the comment id Instagram returned or its error.
- ``media_permalinks``: a media id's permalink, read once (for the deep links' shortcodes).

The vote is read from the channel's city collector (``comment_collectors``): its answers are the comments
that ask for a city. Likes are read live from Instagram whenever the vote is asked for.

Follow-up steps are claimed before their Graph call and keep the id Instagram returns, so a step is never
sent twice. A call that may have posted (a timeout, a 5xx) leaves the step "unsure", and the comments are
read for our text before anything is sent again.
"""

from __future__ import annotations

import asyncio
import html
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, BinaryIO, Protocol
from urllib.parse import quote

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.config import Settings
from app.logger import get_logger
from app.services import comment_collector_store as collectors
from app.services import comment_collectors as cc
from app.services import voted_city as vc
from app.services.email_service import resolve_owner_email, send_email
from app.services.errors import get_error_service
from app.services.media_permalinks import permalinks
from app.timezone import assume_utc, to_ist_iso

logger = get_logger(__name__)

COLLECTOR_NAME = "City requests"
MAX_ATTEMPTS = 3
# A step claimed this long ago without an outcome died mid-call (a restart): check before sending again.
CLAIM_STALE = timedelta(minutes=10)
DOWNLOAD_TTL = 24 * 3600
EMAIL_DOWNLOAD_TTL = 7 * 24 * 3600  # R2's longest signed link; the owner may post days later
OPEN = ("ready", "live")
_FEATURE = "Voted-city reels"


class VoteError(ValueError):
    """The request can't be done as asked; the message says why, for the caller."""


class NotFoundError(LookupError):
    pass


class Graph(Protocol):
    def get_comment_like_counts(self, comment_ids: list[str]) -> dict[str, int]: ...
    def get_permalinks(self, media_ids: list[str]) -> dict[str, str]: ...
    def get_media_page(self, ig_user_id: str, *, limit: int = 25, after: str | None = None) -> dict[str, Any]: ...
    def post_comment(self, media_id: str, message: str) -> str: ...
    def reply_to_comment(self, comment_id: str, message: str) -> str: ...
    def get_media_comments(self, media_id: str) -> list[dict[str, Any]]: ...
    def get_thread(self, comment_id: str, media_id: str = "") -> dict[str, Any]: ...


# ---- The vote -------------------------------------------------------------------------------------------


async def vote_collector(db: AsyncIOMotorDatabase, channel_id: str) -> cc.Collector:
    """The channel's city collector: the one named "City requests"."""
    for c in await collectors.list_collectors(db, channel_id):
        if cc.fold(c.name) == cc.fold(COLLECTOR_NAME):
            return c
    raise NotFoundError(f"this channel has no '{COLLECTOR_NAME}' collector to count the vote from")


async def resolve_city(db: AsyncIOMotorDatabase, channel_id: str, city: str) -> tuple[str, str]:
    """(key, label) for a city as asked for, through the city collector's merges ("HK" is Hong Kong)."""
    label = cc.clean_label(city)
    merges: dict[str, str] = {}
    try:
        merges = (await vote_collector(db, channel_id)).merges
    except NotFoundError:
        pass
    return vc.city_key(label, merges)


async def _channel(db: AsyncIOMotorDatabase, channel_id: str) -> dict[str, Any]:
    doc = await db.channels.find_one({"channel_id": channel_id})
    if not doc:
        raise NotFoundError("Channel not found")
    return dict(doc)


async def done_cities(db: AsyncIOMotorDatabase, channel_id: str) -> list[dict[str, Any]]:
    return await db.voted_cities.find({"channel_id": channel_id}, {"_id": 0}).sort("done_at", -1).to_list(None)


async def mark_done(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    city: str,
    now: datetime,
    *,
    media_id: str | None = None,
    kit_id: str | None = None,
    note: str | None = None,
    comment_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Leave *city* out of the vote from now on. Marking it again keeps the first record."""
    key, label = await resolve_city(db, channel_id, city)
    if not key:
        raise VoteError("which city?")
    doc = {
        "channel_id": channel_id,
        "city": label,
        "city_key": key,
        "media_id": media_id,
        "kit_id": kit_id,
        "note": note,
        "comment_ids": list(comment_ids),
        "done_at": now,
    }
    existing = await db.voted_cities.find_one({"channel_id": channel_id, "city_key": doc["city_key"]}, {"_id": 0})
    if existing:
        return dict(existing)
    try:
        await db.voted_cities.insert_one(dict(doc))
    except DuplicateKeyError:
        pass
    return doc


async def unmark_done(db: AsyncIOMotorDatabase, channel_id: str, city: str) -> bool:
    key, _ = await resolve_city(db, channel_id, city)
    result = await db.voted_cities.delete_one({"channel_id": channel_id, "city_key": key})
    return bool(result.deleted_count)


def _with_links(comment: dict[str, Any], links: dict[str, str]) -> dict[str, Any]:
    code = vc.shortcode(links.get(comment["media_id"]))
    return {
        **comment,
        "shortcode": code,
        "deep_link": vc.deep_link(code, comment["comment_id"]),
        "commented_at": to_ist_iso(assume_utc(comment["commented_at"]))
        if isinstance(comment.get("commented_at"), datetime)
        else None,
    }


async def tally(
    db: AsyncIOMotorDatabase, graph: Graph | None, channel_id: str, now: datetime, *, live: bool = True
) -> dict[str, Any]:
    """The vote, most-liked city first, with every requesting comment ranked by likes.

    *live* reads every comment's likes from Instagram first (about one call per 25 comments) and raises if
    Instagram can't be read, rather than count stale numbers. ``live=False`` counts the likes last kept.
    """
    channel = await _channel(db, channel_id)
    collector = await vote_collector(db, channel_id)
    rows = await db.collector_answers.find(
        {"collector_id": collector.id, "comment_gone": {"$ne": True}}, {"_id": 0}
    ).to_list(None)
    if live:
        if graph is None:
            raise VoteError("no Instagram connection to read live likes with")
        likes, gone = await collectors.read_likes(db, graph, sorted({str(r["comment_id"]) for r in rows}), now)
        gone_ids = set(gone)
        for r in rows:
            cid = str(r["comment_id"])
            if cid in likes:
                r["like_count"] = likes[cid]
            if cid in gone_ids:
                r["comment_gone"] = True
    done = await done_cities(db, channel_id)
    groups = vc.city_groups(
        rows, collector.merges, [d["city_key"] for d in done], [str(channel.get("instagram_username") or "")]
    )
    links = await permalinks(db, graph, [c["media_id"] for g in groups for c in [g["top"], *g["others"]]])
    for g in groups:
        g["top"] = _with_links(g["top"], links)
        g["others"] = [_with_links(c, links) for c in g["others"]]
    return {
        "channel_id": channel_id,
        "collector_id": collector.id,
        "live": live,
        "read_at": to_ist_iso(now),
        "cities": groups,
        "done": [{**d, "done_at": to_ist_iso(assume_utc(d["done_at"]))} for d in done],
    }


# ---- Kits -----------------------------------------------------------------------------------------------


def _kit_link(settings: Settings, kit: dict[str, Any]) -> str:
    base = settings.ANALYZER_PUBLIC_URL.rstrip("/")
    return f"{base}/voted/{quote(kit['channel_id'])}/{kit['kit_id']}"


def _log(event: str, detail: str, now: datetime, ok: bool = True, step: str | None = None) -> dict[str, Any]:
    return {"at": now, "event": event, "detail": detail, "ok": ok, "step": step}


async def create_kit(
    db: AsyncIOMotorDatabase,
    graph: Graph | None,
    r2: Any,
    settings: Settings,
    channel_id: str,
    *,
    city: str,
    votes: int,
    video: BinaryIO,
    caption_body: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Make the posting kit for *city*'s voted reel, after checking its vote live. Emails the owner.

    *votes* is the like count the reel shows ("You voted. … N likes"). It must not be more than the live
    total, or the reel would claim votes that didn't happen.
    """
    if r2 is None:
        raise VoteError("file storage isn't configured on this server")
    if votes < 1:
        raise VoteError("a voted reel needs at least one like to claim")
    vote = await tally(db, graph, channel_id, now, live=True)
    key, _ = await resolve_city(db, channel_id, city)
    group = next((g for g in vote["cities"] if g["key"] == key), None)
    if group is None:
        if any(d["city_key"] == key for d in vote["done"]):
            raise VoteError(f"{city} was already posted as a voted reel")
        raise VoteError(f"nobody has asked for {city} in the comments")
    if votes > group["likes"]:
        raise VoteError(
            f"the reel says {votes:,} likes but {group['city']} has {group['likes']:,} live likes; "
            "re-render with the live number"
        )
    if await db.voted_reel_kits.find_one({"channel_id": channel_id, "city_key": key, "status": {"$in": list(OPEN)}}):
        raise VoteError(f"{group['city']} already has an open kit; cancel it first")
    caption = vc.compose_caption(group["city"], votes, caption_body)
    channel = await _channel(db, channel_id)
    kit_id = uuid.uuid4().hex[:12]
    object_key = f"voted-kits/{channel_id}/{kit_id}.mp4"
    await asyncio.to_thread(r2.upload_video, video, object_key)
    kit: dict[str, Any] = {
        "kit_id": kit_id,
        "channel_id": channel_id,
        "channel_name": str(channel.get("name") or channel_id),
        "instagram_username": channel.get("instagram_username") or None,
        "city": group["city"],
        "city_key": key,
        "votes": votes,
        "live": {
            "likes": group["likes"],
            "comments": group["comments"],
            "people": group["people"],
            "read_at": now,
        },
        "top": group["top"],
        "candidates": group["others"][: vc.CANDIDATES],
        "caption": caption,
        "first_comment": vc.FIRST_COMMENT,
        "r2_object_key": object_key,
        "filename": f"{cc.fold(group['city']).replace(' ', '-')}-voted.mp4",
        "status": "ready",
        "media_id": None,
        "permalink": None,
        "steps": {},
        "step_order": [],
        "log": [_log("created", f"{votes:,} likes claimed; {group['likes']:,} live", now)],
        "created_at": now,
        "updated_at": now,
    }
    await db.voted_reel_kits.insert_one(dict(kit))
    sent = await _email_ready(db, r2, settings, kit)
    await _append_log(db, kit_id, _log("emailed", "owner emailed" if sent else "email not sent", now, ok=sent), now)
    return await get_kit(db, channel_id, kit_id)


async def get_kit(db: AsyncIOMotorDatabase, channel_id: str, kit_id: str) -> dict[str, Any]:
    kit = await db.voted_reel_kits.find_one({"channel_id": channel_id, "kit_id": kit_id}, {"_id": 0})
    if not kit:
        raise NotFoundError("Kit not found")
    return dict(kit)


async def list_kits(db: AsyncIOMotorDatabase, channel_id: str) -> list[dict[str, Any]]:
    return await db.voted_reel_kits.find({"channel_id": channel_id}, {"_id": 0}).sort("created_at", -1).to_list(None)


async def cancel_kit(db: AsyncIOMotorDatabase, kit: dict[str, Any], now: datetime) -> dict[str, Any]:
    if kit["status"] != "ready":
        raise VoteError("only a kit that isn't live yet can be cancelled")
    result = await db.voted_reel_kits.update_one(
        {"kit_id": kit["kit_id"], "status": "ready"}, {"$set": {"status": "cancelled", "updated_at": now}}
    )
    if result.matched_count:
        await _append_log(db, kit["kit_id"], _log("cancelled", "by hand", now), now)
    return await get_kit(db, kit["channel_id"], kit["kit_id"])


async def _append_log(db: AsyncIOMotorDatabase, kit_id: str, entry: dict[str, Any], now: datetime) -> None:
    kit = await db.voted_reel_kits.find_one({"kit_id": kit_id}, {"log": 1})
    log = list((kit or {}).get("log") or []) + [entry]
    await db.voted_reel_kits.update_one({"kit_id": kit_id}, {"$set": {"log": log, "updated_at": now}})


def kit_out(kit: dict[str, Any], r2: Any) -> dict[str, Any]:
    """The kit for the page and the API: times in IST, a fresh download link."""

    def when(value: Any) -> str | None:
        return to_ist_iso(assume_utc(value)) if isinstance(value, datetime) else None

    out = {k: v for k, v in kit.items() if k != "_id"}
    out["download_url"] = (
        r2.generate_presigned_url(kit["r2_object_key"], DOWNLOAD_TTL, "video/mp4") if r2 is not None else None
    )
    out["created_at"] = when(kit.get("created_at"))
    out["updated_at"] = when(kit.get("updated_at"))
    out["linked_at"] = when(kit.get("linked_at"))
    out["live"] = {**kit.get("live", {}), "read_at": when((kit.get("live") or {}).get("read_at"))}
    out["log"] = [{**e, "at": when(e.get("at"))} for e in kit.get("log") or []]
    out["steps"] = [
        {
            **kit["steps"][name],
            "name": name,
            "at": when(kit["steps"][name].get("at")),
            "claimed_at": when(kit["steps"][name].get("claimed_at")),
        }
        for name in kit.get("step_order") or []
        if name in kit.get("steps", {})
    ]
    return out


# ---- Spotting the live reel -----------------------------------------------------------------------------


async def detect(
    db: AsyncIOMotorDatabase, graph: Graph, channel_id: str, ig_user_id: str, now: datetime, *, dry_run: bool = False
) -> list[dict[str, Any]]:
    """Read the newest posts and link each "You voted." reel to its open kit. Returns what each one is.

    Each "You voted." post comes back as linked to a kit, already handled (its city marked done by hand, or
    its kit live or done), or with no kit to match. *dry_run* reports without linking anything.
    """
    page = await asyncio.to_thread(graph.get_media_page, ig_user_id, limit=25)
    media = [m for m in page.get("data", []) if vc.caption_head(m.get("caption")).startswith("you voted.")]
    if not media:
        return []
    ids = [str(m["id"]) for m in media]
    kits_by_media = {
        str(k["media_id"]): k
        async for k in db.voted_reel_kits.find({"channel_id": channel_id, "media_id": {"$in": ids}}, {"_id": 0})
    }
    done_by_media = {
        str(d["media_id"]): d
        async for d in db.voted_cities.find({"channel_id": channel_id, "media_id": {"$in": ids}}, {"_id": 0})
    }
    ready = (
        await db.voted_reel_kits.find({"channel_id": channel_id, "status": "ready"}).sort("created_at", 1).to_list(None)
    )
    report = []
    # Oldest post first, so two kits for the same city (never allowed open together) can't swap reels.
    for item in sorted(media, key=lambda m: m.get("timestamp") or ""):
        mid = str(item["id"])
        entry: dict[str, Any] = {"media_id": mid, "caption": vc.caption_head(item.get("caption")), "kit_id": None}
        if mid in kits_by_media:
            entry.update(outcome="already linked", kit_id=kits_by_media[mid]["kit_id"])
        elif mid in done_by_media:
            entry.update(outcome="city already marked done", city=done_by_media[mid]["city"])
        else:
            kit = next((k for k in ready if vc.matches_kit(k, item)), None)
            if kit is None:
                entry["outcome"] = "no open kit matches"
            elif dry_run:
                entry.update(outcome="would link", kit_id=kit["kit_id"])
            else:
                linked = await link(db, kit, item, now)
                entry.update(outcome="linked" if linked else "already linked", kit_id=kit["kit_id"])
                ready = [k for k in ready if k["kit_id"] != kit["kit_id"]]
        report.append(entry)
    return report


async def link(db: AsyncIOMotorDatabase, kit: dict[str, Any], item: dict[str, Any], now: datetime) -> bool:
    """The kit's reel is live: tie it to the post and mark the city done. False if it was linked already."""
    mid = str(item["id"])
    claimed = await db.voted_reel_kits.update_one(
        {"kit_id": kit["kit_id"], "status": "ready"},
        {
            "$set": {
                "status": "live",
                "media_id": mid,
                "permalink": item.get("permalink"),
                "linked_at": now,
                "updated_at": now,
            }
        },
    )
    if not claimed.matched_count:
        return False
    await _append_log(db, kit["kit_id"], _log("linked", f"live as media {mid}", now), now)
    await mark_done(db, kit["channel_id"], kit["city"], now, media_id=mid, kit_id=kit["kit_id"])
    logger.info("[VotedCity] Kit %s (%s) is live as media %s", kit["kit_id"], kit["city"], mid)
    return True


async def link_by_hand(
    db: AsyncIOMotorDatabase, graph: Graph, kit: dict[str, Any], ig_user_id: str, media_id: str, now: datetime
) -> dict[str, Any]:
    """Link the kit to a post the detector didn't match. The post must be among the account's newest."""
    if kit["status"] != "ready":
        raise VoteError(f"this kit is {kit['status']}, not waiting for its reel")
    page = await asyncio.to_thread(graph.get_media_page, ig_user_id, limit=25)
    item = next((m for m in page.get("data", []) if str(m.get("id")) == media_id), None)
    if item is None:
        raise VoteError("that post isn't among the account's 25 newest")
    if await db.voted_reel_kits.find_one({"media_id": media_id}):
        raise VoteError("that post is already linked to a kit")
    await link(db, kit, item, now)
    return await get_kit(db, kit["channel_id"], kit["kit_id"])


# ---- Following up on a live reel ------------------------------------------------------------------------


async def plan_steps(db: AsyncIOMotorDatabase, graph: Graph, kit: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Set the kit's follow-up steps once: the first comment, then thank-you replies to the top requesters
    whose comments are still up. Returns the kit as stored."""
    if kit.get("step_order"):
        return kit
    candidates = kit.get("candidates") or []
    gone: list[str] = []
    if candidates:
        try:
            _, gone = await collectors.read_likes(db, graph, [c["comment_id"] for c in candidates], now)
        except Exception as exc:  # noqa: BLE001 - a deleted comment then fails its own step, visibly
            logger.warning("[VotedCity] Checking requesters for kit %s failed: %s", kit["kit_id"], exc)
    repliers = vc.pick_repliers(candidates, kit.get("top") or {}, gone)
    steps: dict[str, dict[str, Any]] = {
        "first_comment": {"kind": "comment", "target": kit["media_id"], "text": kit["first_comment"]}
    }
    for i, person in enumerate(repliers):
        steps[f"reply_{i + 1}"] = {
            "kind": "reply",
            "target": person["comment_id"],
            "username": person.get("username"),
            "text": vc.reply_text(i, kit["city"]),
        }
    for step in steps.values():
        step.update(state="pending", attempts=0, comment_id=None, error=None, at=None)
    order = list(steps)
    await db.voted_reel_kits.update_one(
        {"kit_id": kit["kit_id"], "step_order": []},
        {"$set": {"steps": steps, "step_order": order, "updated_at": now}},
    )
    return await get_kit(db, kit["channel_id"], kit["kit_id"])


async def _set_step(
    db: AsyncIOMotorDatabase, kit_id: str, name: str, state_was: str | None, fields: dict[str, Any]
) -> bool:
    query: dict[str, Any] = {"kit_id": kit_id}
    if state_was is not None:
        query[f"steps.{name}.state"] = state_was
    result = await db.voted_reel_kits.update_one(query, {"$set": {f"steps.{name}.{k}": v for k, v in fields.items()}})
    return bool(result.matched_count)


async def _find_ours(graph: Graph, step: dict[str, Any], own: str) -> str | None:
    """The id of our comment with the step's text, if it got posted after all."""
    want = " ".join(step["text"].split())
    if step["kind"] == "comment":
        found = await asyncio.to_thread(graph.get_media_comments, step["target"])
    else:
        found = (await asyncio.to_thread(graph.get_thread, step["target"])).get("replies", [])
    for c in found:
        if str(c.get("author") or "").casefold() == own.casefold() and " ".join(str(c.get("text")).split()) == want:
            return str(c["comment_id"])
    return None


async def advance(
    db: AsyncIOMotorDatabase, graph: Graph, kit: dict[str, Any], own_username: str, now: datetime
) -> dict[str, Any]:
    """Take one follow-up step on a live kit (so the replies go out a minute apart), then settle its status.

    Returns the kit as stored afterwards.
    """
    kit = await plan_steps(db, graph, kit, now)
    kit_id = kit["kit_id"]
    for name in kit["step_order"]:
        step = kit["steps"][name]
        state = step["state"]
        claimed_at = step.get("claimed_at")
        if (
            state == "claimed"
            and isinstance(claimed_at, datetime)
            and assume_utc(now) - assume_utc(claimed_at) > CLAIM_STALE
        ):
            await _set_step(db, kit_id, name, "claimed", {"state": "unsure"})
            state = "unsure"
        if state == "unsure":
            try:
                ours = await _find_ours(graph, step, own_username)
            except Exception as exc:  # noqa: BLE001 - can't check yet: leave it unsure, never resend blind
                logger.warning("[VotedCity] Checking step %s of kit %s failed: %s", name, kit_id, exc)
                break
            if ours:
                await _set_step(db, kit_id, name, "unsure", {"state": "done", "comment_id": ours, "at": now})
                await _append_log(db, kit_id, _log("posted", f"{name}: found as {ours}", now, step=name), now)
                break
            state = "pending" if step["attempts"] < MAX_ATTEMPTS else "failed"
            await _set_step(db, kit_id, name, "unsure", {"state": state})
            if state == "failed":
                await _append_log(
                    db, kit_id, _log("failed", f"{name}: not posted after {MAX_ATTEMPTS} tries", now, False, name), now
                )
                continue
        if state == "claimed":
            break  # in flight: the steps go out in order
        if state != "pending":
            continue
        attempts = int(step["attempts"]) + 1
        if not await _set_step(
            db, kit_id, name, "pending", {"state": "claimed", "claimed_at": now, "attempts": attempts}
        ):
            break  # another pass took it
        await _post_step(db, graph, kit_id, name, step, attempts, now)
        break  # one post per pass
    return await _settle(db, kit_id, now)


async def _post_step(
    db: AsyncIOMotorDatabase, graph: Graph, kit_id: str, name: str, step: dict[str, Any], attempts: int, now: datetime
) -> None:
    try:
        if step["kind"] == "comment":
            new_id = await asyncio.to_thread(graph.post_comment, step["target"], step["text"])
        else:
            new_id = await asyncio.to_thread(graph.reply_to_comment, step["target"], step["text"])
    except Exception as exc:  # noqa: BLE001 - every outcome is kept on the kit
        detail = f"{name}: {exc}"
        if vc.target_gone(exc) and step["kind"] == "reply":
            await _set_step(db, kit_id, name, "claimed", {"state": "skipped", "error": "comment deleted", "at": now})
            await _append_log(db, kit_id, _log("skipped", f"{name}: the comment was deleted", now, step=name), now)
            return
        if vc.refused(exc):
            state = "failed" if attempts >= MAX_ATTEMPTS else "pending"
        else:
            state = "unsure"  # may have posted: checked before any retry
        await _set_step(db, kit_id, name, "claimed", {"state": state, "error": str(exc), "at": now})
        await _append_log(db, kit_id, _log("error", detail, now, ok=False, step=name), now)
        logger.warning("[VotedCity] Step %s of kit %s: %s (now %s)", name, kit_id, exc, state)
        return
    await _set_step(
        db, kit_id, name, "claimed", {"state": "done", "comment_id": new_id or None, "error": None, "at": now}
    )
    await _append_log(db, kit_id, _log("posted", f"{name}: {new_id}", now, step=name), now)


async def _settle(db: AsyncIOMotorDatabase, kit_id: str, now: datetime) -> dict[str, Any]:
    kit = await db.voted_reel_kits.find_one({"kit_id": kit_id}, {"_id": 0})
    assert kit is not None
    states = [kit["steps"][n]["state"] for n in kit.get("step_order") or []]
    status = kit["status"]
    if states and all(s in ("done", "skipped") for s in states):
        status = "done"
    elif any(s == "failed" for s in states) and not any(s in ("pending", "claimed", "unsure") for s in states):
        status = "attention"
    if status != kit["status"]:
        await db.voted_reel_kits.update_one({"kit_id": kit_id}, {"$set": {"status": status, "updated_at": now}})
        kit["status"] = status
    return dict(kit)


async def retry_failed(db: AsyncIOMotorDatabase, kit: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Send the failed steps again (each checked for an earlier post first)."""
    failed = [n for n in kit.get("step_order") or [] if kit["steps"][n]["state"] == "failed"]
    if not failed:
        raise VoteError("no failed steps to retry")
    for name in failed:
        await _set_step(db, kit["kit_id"], name, "failed", {"state": "unsure", "attempts": 0})
    await db.voted_reel_kits.update_one(
        {"kit_id": kit["kit_id"]}, {"$set": {"status": "live", "attention_emailed": False, "updated_at": now}}
    )
    await _append_log(db, kit["kit_id"], _log("retry", ", ".join(failed), now), now)
    return await get_kit(db, kit["channel_id"], kit["kit_id"])


# ---- Telling the owner ----------------------------------------------------------------------------------


async def _email_ready(db: AsyncIOMotorDatabase, r2: Any, settings: Settings, kit: dict[str, Any]) -> bool:
    page = _kit_link(settings, kit)
    download = r2.generate_presigned_url(kit["r2_object_key"], EMAIL_DOWNLOAD_TTL, "video/mp4")
    top = kit["top"]
    deep = top.get("deep_link") or "(no link: the reel's permalink couldn't be read)"
    account = f"@{kit['instagram_username']}" if kit.get("instagram_username") else kit["channel_name"]
    subject = f"Voted reel ready: {kit['city']} on {account}"
    text = "\n".join(
        [
            f"{kit['city']} won the vote: {kit['live']['likes']:,} likes over {kit['live']['comments']} comments.",
            "",
            f"1. Save the video: {download}",
            f"2. Open the top comment (@{top.get('username')}, {top.get('likes')} likes) and reply with the reel: {deep}",
            "3. Paste the caption below.",
            "",
            f"Kit page: {page}",
            "",
            "Caption:",
            kit["caption"],
            "",
            "Once it's live, the first comment and the thank-you replies go out on their own.",
        ]
    )
    body = (
        f"<p style='font-size:18px'><strong>{html.escape(kit['city'])}</strong> won the vote: "
        f"{kit['live']['likes']:,} likes over {kit['live']['comments']} comments.</p>"
        f"<p><a href='{html.escape(page, quote=True)}' style='display:inline-block;padding:10px 18px;"
        "background:#3b82f6;color:#fff;border-radius:8px;text-decoration:none;font-weight:600'>Open the kit</a></p>"
        f"<ol><li><a href='{html.escape(download, quote=True)}'>Save the video</a></li>"
        f"<li>Reply with the reel to @{html.escape(str(top.get('username')))} ({top.get('likes')} likes): "
        f"<a href='{html.escape(deep, quote=True)}'>open the comment</a></li><li>Paste the caption:</li></ol>"
        f"<pre style='white-space:pre-wrap'>{html.escape(kit['caption'])}</pre>"
        "<p>Once it's live, the first comment and the thank-you replies go out on their own.</p>"
    )
    return await send_email(settings, await resolve_owner_email(db, settings), subject, text, body)


async def report_attention(db: AsyncIOMotorDatabase, settings: Settings, kit: dict[str, Any], now: datetime) -> None:
    """Tell the owner (once per failure) that a follow-up step needs them, and log it as an error."""
    if kit.get("status") != "attention" or kit.get("attention_emailed"):
        return
    failed = [
        f"{n}: {kit['steps'][n].get('error') or 'failed'}"
        for n in kit["step_order"]
        if kit["steps"][n]["state"] == "failed"
    ]
    await db.voted_reel_kits.update_one({"kit_id": kit["kit_id"]}, {"$set": {"attention_emailed": True}})
    await get_error_service(db).log_error(
        feature=_FEATURE,
        message=f"Voted reel {kit['city']}: {len(failed)} follow-up step(s) failed",
        context={"kit_id": kit["kit_id"], "channel_id": kit["channel_id"], "failed": failed},
    )
    page = _kit_link(settings, kit)
    text = "\n".join([f"These follow-ups on the {kit['city']} voted reel didn't go out:", *failed, "", f"Kit: {page}"])
    sent = await send_email(
        settings, await resolve_owner_email(db, settings), f"Voted reel {kit['city']}: follow-up failed", text
    )
    await _append_log(
        db, kit["kit_id"], _log("emailed", "failure emailed" if sent else "failure email not sent", now, sent), now
    )


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    await db.voted_cities.create_index([("channel_id", 1), ("city_key", 1)], unique=True)
    await db.voted_cities.create_index("media_id")
    await db.voted_reel_kits.create_index("kit_id", unique=True)
    await db.voted_reel_kits.create_index([("channel_id", 1), ("status", 1), ("created_at", 1)])
    await db.voted_reel_kits.create_index("media_id")
    await db.media_permalinks.create_index("media_id", unique=True)
