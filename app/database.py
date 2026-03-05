from __future__ import annotations

"""MongoDB connection lifecycle and helpers.

Uses Motor (async driver) with a single client created at startup and shared
across all requests.  Index creation runs once during the lifespan event.
"""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

# Module-level reference – set during startup, closed during shutdown.
_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


async def connect_db(mongodb_uri: str, db_name: str) -> AsyncIOMotorDatabase:
    """Create the Motor client, store references, and build indexes."""
    global _client, _db

    _client = AsyncIOMotorClient(mongodb_uri)
    _db = _client[db_name]

    # ---------- indexes ----------
    await _db.channels.create_index("channel_id", unique=True)
    await _db.videos.create_index(
        [("channel_id", 1), ("status", 1)],
    )
    await _db.videos.create_index("video_id", unique=True)
    await _db.posting_queue.create_index(
        [("channel_id", 1), ("position", 1)],
    )
    await _db.schedule_queue.create_index(
        [("channel_id", 1), ("position", 1)],
    )
    await _db.categories.create_index(
        [("channel_id", 1), ("status", 1), ("score", -1)],
    )
    await _db.analysis.create_index("channel_id", unique=True)
    await _db.analysis_history.create_index(
        [("channel_id", 1), ("created_at", -1)],
    )

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
