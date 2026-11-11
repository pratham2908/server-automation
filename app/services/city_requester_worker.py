"""Watches @themapworld-style channels for a new city meet reel, then tells everyone who asked for that city.

The newest posts are read every few minutes (one call) on each channel with the feature on. A run sends
one reply every few seconds, so it gets its own task rather than holding up another worker's loop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.config import Settings
from app.logger import get_logger
from app.services import city_requester_store as store
from app.services.instagram_tokens import PREFER_FOR_REPLYING
from app.timezone import now_ist

logger = get_logger(__name__)

TICK_SECONDS = 60
DETECT_EVERY = timedelta(minutes=5)


async def connection(db: AsyncIOMotorDatabase, manager: Any, channel_id: str) -> tuple[Any, str, str] | None:
    """(service, Instagram user id, our username): the token we reply with, which reads the feed too."""
    resolved = await manager.get_service_and_user_id(channel_id, prefer=PREFER_FOR_REPLYING) if manager else None
    if resolved is None:
        return None
    channel = await db.channels.find_one({"channel_id": channel_id}, {"instagram_username": 1}) or {}
    return resolved[0], str(resolved[1]), str(channel.get("instagram_username") or "")


async def run_pass(
    db: AsyncIOMotorDatabase, manager: Any, settings: Settings, now: datetime, last_detect: dict[str, datetime]
) -> None:
    for channel_id in await store.channels_on(db):
        conn = await connection(db, manager, channel_id)
        if conn is None:
            logger.warning("[CityRequesters] No Instagram connection for %s", channel_id)
            continue
        graph, ig_user_id, own = conn
        previous = last_detect.get(channel_id)
        if previous is None or now - previous >= DETECT_EVERY:
            last_detect[channel_id] = now
            try:
                await store.detect(db, graph, channel_id, ig_user_id, now)
            except Exception as exc:  # noqa: BLE001 - the next check comes round in a few minutes
                logger.warning("[CityRequesters] Reading %s's newest posts failed: %s", channel_id, exc)
        running = (
            await db.city_requester_jobs.find({"channel_id": channel_id, "status": store.JOB_RUNNING}, {"_id": 0})
            .sort("created_at", 1)
            .to_list(None)
        )
        for job in running:
            after = await store.run_job(db, graph, job, own, now_ist)
            await store.report_stop(db, settings, after)
            if after.get("status") == store.JOB_STOPPED:
                break  # an Instagram error stops this channel's replies until someone retries


async def run_city_requester_worker(db: AsyncIOMotorDatabase, manager: Any, settings: Settings) -> None:
    await store.ensure_indexes(db)
    last_detect: dict[str, datetime] = {}
    logger.info("[CityRequesters] Worker started (tick %ds)", TICK_SECONDS)
    while True:
        try:
            await run_pass(db, manager, settings, now_ist(), last_detect)
        except asyncio.CancelledError:
            logger.info("[CityRequesters] Worker shutting down")
            break
        except Exception:
            logger.exception("[CityRequesters] Worker pass failed")
        await asyncio.sleep(TICK_SECONDS)
