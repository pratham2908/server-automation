"""Fetch a channel's headline numbers from its platform and record them.

Shared by the daily growth cron and the per-channel auto-sync, so both write
the same snapshot and cached profile fields from one implementation.
"""

from __future__ import annotations

import asyncio
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import update_channel_task_status
from app.logger import get_logger
from app.services.channel_profile import build_profile_update, name_changed, persist_profile_update
from app.services.growth_tracking import GrowthTrackingService

logger = get_logger(__name__)


async def refresh_channel_stats(
    db: AsyncIOMotorDatabase,
    channel: dict[str, Any],
    youtube_service_manager: Any,
    instagram_service_manager: Any,
    growth_service: GrowthTrackingService,
) -> bool:
    """Refresh subscribers/views/media counts and the cached profile for one channel.

    Returns ``False`` when the channel has no usable platform credential, in which
    case nothing is written. Platform errors propagate to the caller.
    """
    channel_id = channel["channel_id"]
    platform = channel.get("platform", "youtube")
    subs, views = 0, 0
    metadata: dict[str, Any] = {}
    info: dict[str, Any] | None = None

    # The platform clients are synchronous HTTP; run them off the event loop so a
    # slow response does not stall every other request the server is handling.
    if platform == "youtube":
        yt_service = await youtube_service_manager.get_service(channel_id)
        if yt_service:
            info = await asyncio.to_thread(yt_service.get_channel_info, channel.get("youtube_channel_id", ""))
            subs = info.get("subscriber_count", 0)
            views = info.get("view_count", 0)
            metadata = {"video_count": info.get("video_count", 0)}

    elif platform == "instagram":
        ig_service = await instagram_service_manager.get_service(channel_id)
        if ig_service:
            info = await asyncio.to_thread(ig_service.get_account_info, channel.get("instagram_user_id", ""))
            subs = info.get("followers_count", 0)
            # Instagram has no single account-level view total.
            views = 0
            metadata = {"media_count": info.get("media_count", 0)}

    if info is None:
        # No usable platform credential. Recording a zero snapshot here would put
        # a fake collapse to 0 followers into the growth chart, so skip instead.
        logger.warning("Channel stats: skipping '%s' — no %s credential available", channel_id, platform)
        return False

    # One snapshot per day (upserted by date), so refreshing several times a day
    # keeps the latest reading rather than adding points to the growth chart.
    await growth_service.record_snapshot(channel_id, platform, subs, views, metadata)

    # The payload already carries the display name, avatar, and description, so
    # refreshing the cached profile costs no extra API call.
    current_name = channel.get("name", "")
    profile = build_profile_update(platform, info, current_name)
    await persist_profile_update(db, channel_id, profile)
    if name_changed(profile, current_name):
        logger.success(
            "Channel '%s' renamed on %s: %r -> %r",
            channel_id,
            platform,
            current_name,
            profile["name"],
        )

    await update_channel_task_status(db, channel_id, "growth_tracking")
    return True
