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
    await _db.analysis_history.create_index(
        [("channel_id", 1), ("video_id", 1)],
        unique=True,
    )
    await _db.content_params.create_index(
        [("channel_id", 1), ("name", 1)],
        unique=True,
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


async def get_content_schema_for_prompt(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    category: str | None = None,
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
    return [
        {
            "name": d["name"],
            "description": d.get("description", ""),
            "values": [v["value"] for v in d.get("values", [])],
            "unique": d.get("unique", False),
        }
        for d in docs
    ]
