"""Which reels still need their comment pinned, and which have been.

Instagram offers no way to pin a comment through the API, and no way to read whether one is pinned
(probed on both logins, 30 Oct 2026: no pin field exists to read or write). So pinning stays a manual step
in the app, and this keeps the record of it: every reel whose first comment we posted is *expected* to have
that comment pinned, and the owner ticks it off once it is.

The state lives on the video: ``pin_status`` is ``done`` or ``skipped`` (no pin wanted for this reel), and
absent while the pin is still owed. Only reels whose comment was posted since ``PIN_TRACKING_SINCE`` are
tracked: older ones were never part of this routine.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services import voted_city as vc
from app.services.voted_city_store import permalinks
from app.services.voted_city_worker import graph_for
from app.timezone import IST, assume_utc, now_ist, to_ist_iso

DONE = "done"
SKIPPED = "skipped"
STATUSES = (DONE, SKIPPED)
PIN_TRACKING_SINCE = datetime(2026, 11, 8, tzinfo=IST)
RECENT_FINISHED = 20


def tracked_filter(since: datetime = PIN_TRACKING_SINCE) -> dict[str, Any]:
    return {
        "instagram_media_id": {"$ne": None},
        "first_comment_status": "posted",
        "first_comment_id": {"$ne": None},
        "first_comment_posted_at": {"$gte": since},
    }


def pin_item(video: Mapping[str, Any], permalink: str | None) -> dict[str, Any]:
    code = vc.shortcode(permalink)
    posted = video.get("first_comment_posted_at")
    return {
        "video_id": video["video_id"],
        "title": (video.get("title") or "Untitled reel").strip(),
        "comment_text": (video.get("first_comment") or "").strip(),
        "posted_at": to_ist_iso(assume_utc(posted)) if isinstance(posted, datetime) else None,
        "reel_url": permalink,
        "comment_url": vc.deep_link(code, str(video["first_comment_id"])),
        "status": video.get("pin_status"),
    }


async def _channel_block(
    db: AsyncIOMotorDatabase, manager: Any, channel_id: str, videos: list[dict[str, Any]]
) -> dict[str, Any]:
    channel = await db.channels.find_one({"channel_id": channel_id}) or {}
    conn = await graph_for(db, manager, channel_id)
    links = await permalinks(db, conn[0] if conn else None, [str(v["instagram_media_id"]) for v in videos])
    items = [pin_item(v, v.get("instagram_permalink") or links.get(str(v["instagram_media_id"]))) for v in videos]
    pending = [i for i in items if i["status"] not in STATUSES]
    finished = [i for i in items if i["status"] in STATUSES]
    return {
        "channel_id": channel_id,
        "name": channel.get("name") or channel_id,
        "handle": channel.get("handle") or "",
        "pending": pending,
        "finished": finished[:RECENT_FINISHED],
        "finished_count": len(finished),
    }


async def pins_overview(db: AsyncIOMotorDatabase, manager: Any, since: datetime = PIN_TRACKING_SINCE) -> dict[str, Any]:
    """Every channel with tracked reels: what is still owed (newest first) and what was finished."""
    by_channel: dict[str, list[dict[str, Any]]] = {}
    async for v in db.videos.find(tracked_filter(since)).sort("first_comment_posted_at", -1):
        by_channel.setdefault(v["channel_id"], []).append(v)
    blocks = [await _channel_block(db, manager, cid, vids) for cid, vids in sorted(by_channel.items())]
    blocks.sort(key=lambda b: -len(b["pending"]))
    return {"channels": blocks, "pending_total": sum(len(b["pending"]) for b in blocks)}


async def set_pin_status(db: AsyncIOMotorDatabase, video_id: str, status: str | None) -> bool:
    """Tick (``done`` / ``skipped``) or un-tick (``None``) a reel. False when no such reel."""
    if status is None:
        update: dict[str, Any] = {"$unset": {"pin_status": "", "pin_status_at": ""}}
    else:
        update = {"$set": {"pin_status": status, "pin_status_at": now_ist()}}
    result = await db.videos.update_one({"video_id": video_id, **tracked_filter()}, update)
    return result.matched_count > 0
