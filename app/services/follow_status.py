"""Whether the people talking to us follow us, asked of Instagram and kept on their conversation.

Instagram answers ``is_user_follow_business`` only for someone who has written to the account or tapped
one of its buttons — for anyone else it refuses with code 230 ("user consent is required"). So this
asks only about people with a message or tap of theirs on record, at most once a day each, and keeps
the answer on ``dm_conversations`` (``follows_us``, ``follows_checked_at``). A refusal is kept as
unknown, not as "doesn't follow".
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import requests
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.instagram import graph_error_code, graph_error_message
from app.services.instagram_tokens import PROVIDER_INSTAGRAM
from app.timezone import assume_utc

logger = get_logger(__name__)

FOLLOWS_FIELD = "follows_us"
CHECKED_FIELD = "follows_checked_at"
RECHECK_AFTER = timedelta(hours=24)
# Instagram allows about 2 calls a second per account on these endpoints.
CALL_SPACING_SECONDS = 0.5
MAX_PER_RUN = 300
NO_CONSENT = 230


def due(conversation: Mapping[str, Any], now: datetime) -> bool:
    checked = conversation.get(CHECKED_FIELD)
    return not isinstance(checked, datetime) or now - assume_utc(checked) >= RECHECK_AFTER


async def refresh_channel(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    service: Any,
    now: datetime,
    *,
    force: bool = False,
    spacing: float = CALL_SPACING_SECONDS,
) -> dict[str, int]:
    """Ask about everyone who has written or tapped and wasn't asked in the last day. Returns counts."""
    writers = set(await db.dm_messages.distinct("peer_id", {"channel_id": channel_id, "from_me": False}))
    conversations = await db.dm_conversations.find(
        {"channel_id": channel_id, "peer_id": {"$in": sorted(writers)}},
        {"peer_id": 1, CHECKED_FIELD: 1},
    ).to_list(None)
    todo = [c for c in conversations if force or due(c, now)][:MAX_PER_RUN]
    counts = {"checked": 0, "following": 0, "not_following": 0, "unknown": 0}
    for conv in todo:
        peer = str(conv["peer_id"])
        follows: bool | None
        try:
            profile = await asyncio.to_thread(service.messaging_profile, peer)
            value = profile.get("is_user_follow_business")
            follows = value if isinstance(value, bool) else None
        except requests.RequestException as exc:
            if graph_error_code(exc) != NO_CONSENT:
                logger.warning("[Follows] Check of %s on %s failed: %s", peer, channel_id, graph_error_message(exc))
            follows = None
        await db.dm_conversations.update_one(
            {"channel_id": channel_id, "peer_id": peer}, {"$set": {FOLLOWS_FIELD: follows, CHECKED_FIELD: now}}
        )
        counts["checked"] += 1
        counts["following" if follows else "unknown" if follows is None else "not_following"] += 1
        if spacing:
            await asyncio.sleep(spacing)
    return counts


async def refresh_all(db: AsyncIOMotorDatabase, manager: Any, now: datetime) -> dict[str, dict[str, int]]:
    """Every Instagram Login channel with conversations; one channel's failure never stops the rest."""
    results: dict[str, dict[str, int]] = {}
    for channel_id in await db.dm_conversations.distinct("channel_id"):
        resolved = await manager.get_service_and_user_id(channel_id, prefer=PROVIDER_INSTAGRAM)
        if resolved is None or resolved[0].provider != PROVIDER_INSTAGRAM:
            continue
        try:
            results[channel_id] = await refresh_channel(db, channel_id, resolved[0], now)
        except Exception as exc:  # noqa: BLE001 - the next run retries; one channel must not stop the others
            logger.warning("[Follows] Refresh of %s failed: %s", channel_id, exc)
    return results
