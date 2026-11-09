"""Watches for voted-city reels going live, and follows up on them.

While a kit waits for its reel, the account's newest posts are read every few minutes (one call) for a
"You voted." caption. A live kit gets one follow-up step a minute: the first comment, then each thank-you
reply, so they don't land as a burst. Nothing is read when no kit is open.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.config import Settings
from app.logger import get_logger
from app.services import voted_city_store as store
from app.services.instagram_tokens import PROVIDER_FACEBOOK
from app.timezone import now_ist

logger = get_logger(__name__)

TICK_SECONDS = 60
DETECT_EVERY = timedelta(minutes=5)


async def graph_for(db: AsyncIOMotorDatabase, manager: Any, channel_id: str) -> tuple[Any, str, str] | None:
    """(service, Instagram user id, our username) for the channel, or None without a connection."""
    if manager is None:
        return None
    resolved = await manager.get_service_and_user_id(channel_id, prefer=PROVIDER_FACEBOOK)
    if resolved is None:
        return None
    channel = await db.channels.find_one({"channel_id": channel_id}, {"instagram_username": 1}) or {}
    return resolved[0], str(resolved[1]), str(channel.get("instagram_username") or "")


async def run_pass(
    db: AsyncIOMotorDatabase, manager: Any, settings: Settings, now: datetime, last_detect: dict[str, datetime]
) -> None:
    open_kits = await db.voted_reel_kits.find({"status": {"$in": list(store.OPEN)}}, {"_id": 0}).to_list(None)
    for channel_id in sorted({k["channel_id"] for k in open_kits}):
        conn = await graph_for(db, manager, channel_id)
        if conn is None:
            logger.warning("[VotedCity] No Instagram connection for %s", channel_id)
            continue
        graph, ig_user_id, own = conn
        kits = [k for k in open_kits if k["channel_id"] == channel_id]
        previous = last_detect.get(channel_id)
        if any(k["status"] == "ready" for k in kits) and (previous is None or now - previous >= DETECT_EVERY):
            last_detect[channel_id] = now
            try:
                await store.detect(db, graph, channel_id, ig_user_id, now)
            except Exception as exc:  # noqa: BLE001 - best effort: the owner can link it by hand, next check soon
                logger.warning("[VotedCity] Reading %s's newest posts failed: %s", channel_id, exc)
        live = await db.voted_reel_kits.find({"channel_id": channel_id, "status": "live"}, {"_id": 0}).to_list(None)
        for kit in live:
            try:
                after = await store.advance(db, graph, kit, own, now)
                await store.report_attention(db, settings, after, now)
            except Exception:
                logger.exception("[VotedCity] Following up kit %s failed", kit["kit_id"])


async def run_voted_city_worker(db: AsyncIOMotorDatabase, manager: Any, settings: Settings) -> None:
    await store.ensure_indexes(db)
    last_detect: dict[str, datetime] = {}
    logger.info("[VotedCity] Worker started (tick %ds)", TICK_SECONDS)
    while True:
        try:
            await run_pass(db, manager, settings, now_ist(), last_detect)
        except asyncio.CancelledError:
            logger.info("[VotedCity] Worker shutting down")
            break
        except Exception:
            logger.exception("[VotedCity] Worker pass failed")
        await asyncio.sleep(TICK_SECONDS)
