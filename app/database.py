from __future__ import annotations

"""MongoDB connection lifecycle and helpers.

Uses Motor (async driver) with a single client created at startup and shared
across all requests.  Index creation runs once during the lifespan event.
"""

import certifi
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

# Module-level reference – set during startup, closed during shutdown.
_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


async def connect_db(
    mongodb_uri: str,
    db_name: str,
    *,
    create_indexes: bool = True,
) -> AsyncIOMotorDatabase:
    """Create the Motor client, store references, and optionally build indexes.

    Set create_indexes=False for one-off scripts (e.g. backfills) so they only
    open a connection; indexes are assumed to already exist from the main app.
    """
    global _client, _db

    _client = AsyncIOMotorClient(mongodb_uri, tlsCAFile=certifi.where())
    _db = _client[db_name]

    if not create_indexes:
        return _db

    # ---------- indexes ----------
    await _db.channels.create_index("channel_id", unique=True)
    await _db.videos.create_index(
        [("channel_id", 1), ("status", 1)],
    )
    await _db.videos.create_index("video_id", unique=True)
    await _db.videos.create_index("retention.status")
    await _db.videos.create_index("performance.analyzed_at")
    # Backs the "already imported?" dedup check when listing a channel app's videos.
    await _db.videos.create_index([("channel_id", 1), ("source_id", 1), ("source_video_id", 1)], sparse=True)
    # A channel belongs to at most one group, so "who else gets this video" has a
    # single answer. The index makes the lookup by member cheap on every publish.
    await _db.channel_groups.create_index("group_id", unique=True)
    await _db.channel_groups.create_index("channel_ids")
    await _db.video_sources.create_index("source_id", unique=True)
    await _db.video_sources.create_index("channel_id")
    await _db.source_imports.create_index("job_id", unique=True)
    await _db.source_imports.create_index([("channel_id", 1), ("created_at", -1)])
    await _db.source_imports.create_index("status")
    # Auto-scheduler: one run doc per (day, channel); one summary latch per day.
    await _db.auto_scheduler_runs.create_index([("date", 1), ("channel_id", 1)], unique=True)
    await _db.auto_scheduler_summaries.create_index("date", unique=True)
    await _db.posting_queue.create_index(
        [("channel_id", 1), ("position", 1)],
    )
    # Retention-analysis queue: the worker claims the next job by status + position.
    await _db.retention_analysis_queue.create_index([("status", 1), ("position", 1)])
    await _db.retention_analysis_queue.create_index([("channel_id", 1), ("video_id", 1)])
    await _db.schedule_queue.create_index(
        [("channel_id", 1), ("position", 1)],
    )
    await _db.categories.create_index(
        [("channel_id", 1), ("status", 1), ("score", -1)],
    )
    await _db.categories.create_index("id", unique=True)
    await _db.analysis.create_index("channel_id", unique=True)
    await _db.analysis_history.create_index(
        [("channel_id", 1), ("created_at", -1)],
    )
    await _db.analysis_history.create_index(
        [("channel_id", 1), ("video_id", 1)],
        unique=True,
    )
    await _db.content_params.create_index(
        [("channel_id", 1), ("name", 1)],
        unique=True,
    )
    await _db.competitors.create_index(
        [("channel_id", 1), ("youtube_channel_id", 1)],
        unique=True,
    )
    await _db.comment_analysis.create_index(
        [("channel_id", 1), ("platform_video_id", 1)],
        unique=True,
    )
    await _db.comment_analysis.create_index(
        [("channel_id", 1), ("analyzed_at", -1)],
    )
    await _db.comment_analysis.create_index(
        [("channel_id", 1), ("source", 1)],
    )
    await _db.retention_analysis.create_index(
        [("channel_id", 1), ("video_id", 1)],
        unique=True,
    )
    await _db.retention_analysis.create_index(
        [("channel_id", 1), ("analyzed_at", -1)],
    )
    await _db.comment_replies.create_index(
        [("channel_id", 1), ("comment_id", 1)],
        unique=True,
    )
    await _db.comment_replies.create_index(
        [("channel_id", 1), ("replied_at", -1)],
    )
    # AI cost observability — recent-first feed plus per-model/per-task rollups.
    await _db.ai_call_logs.create_index([("timestamp", -1)])
    await _db.ai_call_logs.create_index([("task", 1), ("timestamp", -1)])
    await _db.ai_call_logs.create_index([("model", 1), ("timestamp", -1)])
    await _db.preview_analysis.create_index(
        "expires_at",
        expireAfterSeconds=0,
    )
    await _db.preview_analysis.create_index("preview_id", unique=True)

    await _db.thumbnail_analysis.create_index(
        "expires_at",
        expireAfterSeconds=0,
    )
    await _db.thumbnail_analysis.create_index("analysis_id", unique=True)
    await _db.errors.create_index([("feature", 1), ("resolved", 1)])
    await _db.errors.create_index("timestamp")

    await _db.video_intelligence.create_index(
        [("channel_id", 1), ("source", 1)],
    )
    await _db.video_intelligence.create_index(
        [("channel_id", 1), ("platform_video_id", 1)],
        unique=True,
    )
    await _db.video_intelligence.create_index("intel_id", unique=True)

    return _db


async def close_db() -> None:
    """Gracefully close the Motor client."""
    global _client, _db
    if _client:
        _client.close()
    _client = None
    _db = None


def get_db() -> AsyncIOMotorDatabase:
    """Return the active database handle.

    Raises ``RuntimeError`` if called before ``connect_db``.
    """
    if _db is None:
        raise RuntimeError("Database not initialised – call connect_db first")
    return _db


async def get_youtube_oauth_config(db: AsyncIOMotorDatabase) -> dict | None:
    """Return the YouTube OAuth client credentials from the ``config`` collection."""
    return await db.config.find_one({"key": "youtube_oauth"})


async def get_instagram_oauth_config(db: AsyncIOMotorDatabase) -> dict | None:
    """Return the Instagram/Facebook OAuth app credentials from the ``config`` collection."""
    return await db.config.find_one({"key": "instagram_oauth"})


async def get_content_schema_for_prompt(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    category: str | None = None,
    include_belongs_to: bool = False,
) -> list[dict]:
    """Fetch content param definitions from the ``content_params`` collection
    and return them in the list-of-dicts format that Gemini prompts expect.

    When *category* is provided, only params whose ``belongs_to`` includes
    that category name or ``"all"`` are returned.
    """
    query: dict = {"channel_id": channel_id}
    if category:
        query["$or"] = [
            {"belongs_to": "all"},
            {"belongs_to": category},
        ]

    docs = await db.content_params.find(query).to_list(length=None)

    result = []
    for d in docs:
        param = {
            "name": d["name"],
            "description": d.get("description", ""),
            "values": [v["value"] for v in d.get("values", [])],
            "unique": d.get("unique", False),
        }
        if include_belongs_to:
            param["belongs_to"] = d.get("belongs_to", ["all"])
        result.append(param)

    return result


async def update_channel_task_status(db: AsyncIOMotorDatabase, channel_id: str, task_name: str):
    """Update the last run timestamp for a specific task on a channel."""
    from app.timezone import now_ist

    await db.channels.update_one(
        {"channel_id": channel_id},
        {"$set": {f"last_tasks.{task_name}": now_ist()}},
    )


def not_paused_query() -> dict:
    """Mongo filter selecting channels that background crons should process.

    Use this for every cron-level ``db.channels.find()`` so "active" is
    defined in one place and a new cron inherits the behaviour for free.

    Matches on ``$ne: True`` rather than ``False`` deliberately: the ``paused``
    field was added after these documents were written, so existing channels
    have no such key at all. Filtering on ``{"paused": False}`` would match
    none of them and silently halt the entire system.
    """
    return {"paused": {"$ne": True}}


def is_channel_paused(channel: dict | None) -> bool:
    """Whether background work should skip this channel.

    For callers that already hold the document and cannot use
    ``not_paused_query`` — ``auto_publisher`` looks its channel up per due
    video rather than listing channels.
    """
    if not channel:
        return False
    return bool(channel.get("paused", False))


def get_channel_platform(channel: dict | None) -> str:
    """Resolve the platform for a channel document, with graceful fallback.

    If 'platform' is missing or 'youtube', but 'instagram_user_id' exists
    and 'youtube_channel_id' is missing, it returns 'instagram'.
    """
    if not channel:
        return "youtube"
    platform = channel.get("platform", "youtube")
    if platform == "youtube" and channel.get("instagram_user_id") and not channel.get("youtube_channel_id"):
        return "instagram"
    from typing import cast

    return cast(str, platform)
