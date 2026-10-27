"""Stores Instagram audience demographics and live-story insights, as part of each sync.

The side-effect shell around ``instagram_audience``:

- ``instagram_audience``: one document per channel per day holding every
  demographics breakdown, refreshed at most once a day (24 calls per refresh).
- ``instagram_stories``: one document per story, overwritten on every sync while
  the story is up. Instagram lists a story only during its 24 hours, so a story's
  final numbers are whatever the last sync before it expired read.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import requests
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.instagram import InstagramService
from app.services.instagram_audience import (
    AUDIENCE_METRICS,
    DEMOGRAPHIC_BREAKDOWNS,
    FOLLOWER_TIMEFRAME,
    TIMEFRAMES,
    audience_is_stale,
    data_changed_at,
    parse_demographics,
    parse_metric_values,
    parse_story_navigation,
    story_metrics_for,
)
from app.timezone import now_ist

logger = get_logger(__name__)


async def refresh_audience(db: AsyncIOMotorDatabase, ig: InstagramService, channel_id: str, ig_user_id: str) -> None:
    now = now_ist()
    latest = await db.instagram_audience.find_one({"channel_id": channel_id}, sort=[("fetched_at", -1)])
    if not audience_is_stale((latest or {}).get("fetched_at"), now):
        return
    audiences: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for name, metric in AUDIENCE_METRICS.items():
        # Followers are the same whatever the timeframe, so one pass is enough (see FOLLOWER_TIMEFRAME).
        asked = (FOLLOWER_TIMEFRAME,) if name == "followers" else TIMEFRAMES
        for timeframe in asked:
            for breakdown in DEMOGRAPHIC_BREAKDOWNS:
                body = await asyncio.to_thread(ig.get_demographics, ig_user_id, metric, breakdown, timeframe)
                audiences.setdefault(name, {}).setdefault(timeframe, {})[breakdown] = parse_demographics(body)
        if name == "followers":
            audiences[name] = {timeframe: audiences[name][FOLLOWER_TIMEFRAME] for timeframe in TIMEFRAMES}
    await db.instagram_audience.update_one(
        {"channel_id": channel_id, "date": now.date().isoformat()},
        {
            "$set": {
                "channel_id": channel_id,
                "fetched_at": now,
                "data_changed_at": data_changed_at(latest, audiences, now),
                "audiences": audiences,
            }
        },
        upsert=True,
    )


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        # Graph timestamps look like 2026-10-15T15:44:23+0000.
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


async def refresh_stories(db: AsyncIOMotorDatabase, ig: InstagramService, channel_id: str, ig_user_id: str) -> None:
    stories = await asyncio.to_thread(ig.get_live_stories, ig_user_id)
    metrics = story_metrics_for(ig.provider)
    for story in stories:
        story_id = str(story["id"])
        update: dict[str, Any] = {
            "channel_id": channel_id,
            "story_id": story_id,
            "media_type": story.get("media_type"),
            "thumbnail_url": story.get("thumbnail_url") or story.get("media_url"),
            "permalink": story.get("permalink"),
            "caption": story.get("caption"),
            "posted_at": _parse_timestamp(story.get("timestamp")),
            "fetched_at": now_ist(),
        }
        try:
            body = await asyncio.to_thread(ig.get_story_insights, story_id, metrics)
            update["metrics"] = parse_metric_values(body)
            update["insights_note"] = None
        except requests.HTTPError as exc:
            # A fresh story is refused until enough people have seen it ("Not enough
            # viewers"); keep the last numbers rather than overwrite them with nothing.
            update["insights_note"] = _graph_message(exc)
        navigation = await asyncio.to_thread(ig.get_story_navigation, story_id)
        if navigation is not None:
            update["navigation"] = parse_story_navigation(navigation)
        await db.instagram_stories.update_one(
            {"channel_id": channel_id, "story_id": story_id}, {"$set": update}, upsert=True
        )


def _graph_message(exc: requests.HTTPError) -> str:
    try:
        return str(exc.response.json().get("error", {}).get("message") or exc)[:200]
    except (ValueError, AttributeError):
        return str(exc)[:200]
