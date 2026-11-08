"""The three headline counts for a channel's videos, as database filters.

The dashboard's cross-channel rollup only needs these counts. Fetching every video document to count
three statuses on the client moved hundreds of kilobytes per channel, so the database counts instead.
The filters mirror what the rollup used to compute from the full list (archived videos excluded, as
in the default video list).
"""

from __future__ import annotations

from typing import Any

PULSE_KEYS = ("scheduled", "ready", "attention")


def pulse_filters(channel_id: str) -> dict[str, dict[str, Any]]:
    live = {"channel_id": channel_id, "status": {"$ne": "archived"}}
    return {
        # Instagram and Threads reels wait as "queued" for the server's publisher; YouTube's as "scheduled".
        "scheduled": {"channel_id": channel_id, "status": {"$in": ["scheduled", "queued"]}},
        "ready": {"channel_id": channel_id, "status": "ready"},
        # A failed publish, or a post that vanished from the platform.
        "attention": {
            **live,
            "$or": [
                {"last_failure": {"$exists": True, "$nin": [None, {}, "", False]}},
                {"platform_missing_since": {"$exists": True, "$ne": None}},
            ],
        },
    }
