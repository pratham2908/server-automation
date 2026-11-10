"""Someone shares one of our reels into our DMs: answer with that reel's comment-to-DM message.

People who want what a reel offers ("comment MEET for the tool") often send the reel to us instead of
commenting (owner, 2026-11-10: 15 people on TryAlgoViz did, 3 got an answer by hand). If the reel has a
comment-to-DM rule, they now get the same DM a commenter gets, once per person per reel, and not at all
if a comment already got it to them.

Shares are picked up by the comment-DM worker's tick rather than inside the webhook, because naming the
reel may need an Instagram call (its permalink). The reply goes through the DM outbox, which sends it
while Instagram's 24-hour window after their message is still open.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services import dm_outbox
from app.services.comment_dm_store import active_rule, own_ids
from app.services.instagram_tokens import PROVIDER_FACEBOOK
from app.services.media_permalinks import permalinks, shortcode

logger = get_logger(__name__)

FIELD = "reel_share"  # on the dm_messages row: what was done with the share, so it's looked at once
REASON = "reel_share"  # the dm_outbox row's reason
MAX_PER_TICK = 20
# Instagram lets us answer a DM for 24 hours; leave room for the send itself.
ANSWER_WITHIN = dm_outbox.REPLY_WINDOW - timedelta(minutes=30)

QUEUED = "queued"
NOT_A_REEL = "not a reel link"
FROM_US = "sent by us"
NO_CONNECTION = "no Instagram connection"
NOT_OURS = "not one of our reels"
NO_RULE = "no comment-to-DM rule on this reel"
NEEDS_COMMENT = "the reel's rule needs a comment (a city, or a count only)"
ALREADY = "already got this reel's DM"


def outbox_key(channel_id: str, media_id: str, peer_id: str) -> str:
    """One answer per person per reel, however many times they share it."""
    return f"{REASON}:{channel_id}:{media_id}:{peer_id}"


async def _got_it_from_a_comment(db: AsyncIOMotorDatabase, channel_id: str, media_id: str, peer_id: str) -> bool:
    convo = await db.dm_conversations.find_one({"channel_id": channel_id, "peer_id": peer_id}, {"peer_username": 1})
    who: list[dict[str, Any]] = [{"from_id": peer_id}]
    username = str((convo or {}).get("peer_username") or "").lower()
    if username:
        who.append({"person": username})
    found = await db.comment_dm_events.find_one(
        {
            "channel_id": channel_id,
            "media_id": media_id,
            "status": {"$in": ["pending", "sending", "sent"]},
            "$or": who,
        },
        {"_id": 1},
    )
    return found is not None


async def answer_share(
    db: AsyncIOMotorDatabase, manager: Any, message: dict[str, Any], now: datetime, graphs: dict[str, Any]
) -> str:
    """Queue the reel's comment-to-DM message for one shared reel. Returns what happened."""
    code = shortcode(message.get("link"))
    if code is None:
        return NOT_A_REEL
    channel_id = str(message["channel_id"])
    peer_id = str(message["peer_id"])
    channel = await db.channels.find_one({"channel_id": channel_id})
    if channel is None or message.get("from_me") or peer_id in own_ids(dict(channel)):
        return FROM_US
    if channel_id not in graphs:
        resolved = await manager.get_service_and_user_id(channel_id, prefer=PROVIDER_FACEBOOK)
        graphs[channel_id] = resolved[0] if resolved else None
    ids = [
        str(v["instagram_media_id"])
        async for v in db.videos.find(
            {"channel_id": channel_id, "instagram_media_id": {"$nin": [None, ""]}}, {"instagram_media_id": 1}
        )
    ]
    links = await permalinks(db, graphs[channel_id], ids)
    media_id = next((mid for mid, link in links.items() if shortcode(link) == code), None)
    if media_id is None:
        return NOT_OURS if graphs[channel_id] is not None else NO_CONNECTION
    active = await active_rule(db, channel_id, media_id)
    if active is None:
        return NO_RULE
    rule = active.rule
    if rule.tally_only or rule.city_map is not None:
        return NEEDS_COMMENT
    if await _got_it_from_a_comment(db, channel_id, media_id, peer_id):
        return ALREADY
    dm = rule.message()
    queued = await dm_outbox.enqueue(
        db,
        key=outbox_key(channel_id, media_id, peer_id),
        channel_id=channel_id,
        recipient_id=peer_id,
        message=dm,
        check_follow=bool(dm.followers_only_text),
        reason=REASON,
        now=now,
    )
    return QUEUED if queued else ALREADY


async def answer_shared_reels(db: AsyncIOMotorDatabase, manager: Any, now: datetime) -> int:
    """Look at every reel shared into our DMs that hasn't been looked at. Returns how many got a DM queued."""
    shares = (
        await db.dm_messages.find(
            {
                "kind": "share",
                "from_me": False,
                "created_at": {"$gte": now - ANSWER_WITHIN},
                FIELD: {"$exists": False},
            }
        )
        .sort("created_at", 1)
        .limit(MAX_PER_TICK)
        .to_list(MAX_PER_TICK)
    )
    graphs: dict[str, Any] = {}
    queued = 0
    for share in shares:
        try:
            outcome = await answer_share(db, manager, share, now, graphs)
        except Exception as exc:  # noqa: BLE001 - one bad share is logged and skipped, never retried in a loop
            logger.warning("[ReelShare] Answering share %s failed: %s", share.get("message_id"), exc)
            outcome = f"error: {exc}"[:200]
        await db.dm_messages.update_one(
            {"message_id": share["message_id"]}, {"$set": {FIELD: {"outcome": outcome, "at": now}}}
        )
        if outcome == QUEUED:
            queued += 1
            logger.info("[ReelShare] %s shared a reel on %s: its DM is queued", share["peer_id"], share["channel_id"])
    return queued
