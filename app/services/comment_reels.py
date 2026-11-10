"""Reels that got comments lately, with a link that opens each one (and each comment) in Instagram.

Built for pinning: on a phone, the reel link lands in the Instagram app, and a comment link scrolls to that
comment, which is then one press-and-hold from "Pin". Instagram's API cannot pin, so the job here is only to
make the manual step as short as possible.

Only comments from other people count: our own replies and the comment bot's answers are left out, and so
are replies, so a thread is counted once.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services import voted_city as vc
from app.services.voted_city_store import permalinks
from app.services.voted_city_worker import graph_for
from app.timezone import assume_utc, to_ist_iso

SAMPLES = 3
# A pinned comment should say something; a one-word trigger ("meet") or an emoji is not worth surfacing.
MIN_SAMPLE_CHARS = 12


def _says_something(text: str) -> bool:
    """Long enough, and not mostly @-mentions (people tagging friends, which is not a comment worth pinning)."""
    words = text.split()
    if len(text.strip()) < MIN_SAMPLE_CHARS or not words:
        return False
    return sum(w.startswith("@") for w in words) / len(words) < 0.5


def pick_samples(comments: Sequence[dict[str, Any]], limit: int = SAMPLES) -> list[dict[str, Any]]:
    """The longest comments, shown oldest first, skipping anything too short to be worth pinning."""
    worth = [c for c in comments if _says_something(c.get("text") or "")]
    longest = sorted(worth, key=lambda c: -len(c["text"]))[:limit]
    return sorted(longest, key=lambda c: c["commented_at"])


def group_by_media(comments: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in comments:
        if c.get("media_id"):
            grouped[str(c["media_id"])].append(c)
    return dict(grouped)


def reel_row(video: dict[str, Any], comments: Sequence[dict[str, Any]], permalink: str | None) -> dict[str, Any]:
    code = vc.shortcode(permalink)
    published = video.get("published_at")
    return {
        "title": (video.get("title") or "Untitled reel").strip(),
        "published_at": to_ist_iso(assume_utc(published)) if isinstance(published, datetime) else None,
        "views": (video.get("metadata") or {}).get("views"),
        "comment_count": len(comments),
        "last_comment_at": max(c["commented_at"] for c in comments).isoformat(),
        "permalink": permalink,
        "samples": [
            {
                "username": c.get("username") or c.get("author") or "someone",
                "text": c["text"].strip(),
                "link": vc.deep_link(code, str(c["comment_id"])),
            }
            for c in pick_samples(comments)
        ],
    }


async def _channel_block(
    db: AsyncIOMotorDatabase, manager: Any, channel_id: str, since: datetime
) -> dict[str, Any] | None:
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        return None
    cursor = db.comment_log.find(
        {"channel_id": channel_id, "own": {"$ne": True}, "is_reply": {"$ne": True}, "commented_at": {"$gte": since}},
        {"media_id": 1, "comment_id": 1, "username": 1, "author": 1, "text": 1, "commented_at": 1},
    )
    by_media = group_by_media([c async for c in cursor if c.get("text")])
    videos = {
        str(v["instagram_media_id"]): v
        async for v in db.videos.find({"channel_id": channel_id, "instagram_media_id": {"$in": list(by_media)}})
    }
    conn = await graph_for(db, manager, channel_id)
    links = await permalinks(db, conn[0] if conn else None, list(videos))
    reels = [reel_row(videos[m], by_media[m], links.get(m)) for m in videos]
    reels.sort(key=lambda r: r["published_at"] or "", reverse=True)
    return {
        "channel_id": channel_id,
        "name": channel.get("name") or channel_id,
        "handle": channel.get("handle") or "",
        "reels": reels,
        "comments": sum(r["comment_count"] for r in reels),
    }


async def build_comment_reels(
    db: AsyncIOMotorDatabase, manager: Any, channel_ids: Sequence[str], days: int, now: datetime
) -> dict[str, Any]:
    """``now`` is UTC: comment times are stored as naive UTC."""
    since = now.astimezone(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    blocks = [b for cid in channel_ids if (b := await _channel_block(db, manager, cid, since)) and b["reels"]]
    return {
        "days": days,
        "channels": blocks,
        "totals": {
            "reels": sum(len(b["reels"]) for b in blocks),
            "comments": sum(b["comments"] for b in blocks),
        },
    }
