"""How many comment-DMs since a time show a sign of arriving: opened (read receipt), or a tap or reply.

Usage (on the server): PYTHONPATH=. venv/bin/python scripts/seen_test_report.py 2026-11-14T00:00+05:30 [channel ...]

The read-receipt test (owner, 11-13): if most DMs show a sign within a few hours, a follow-up reply can go only
to the people who show none. DMs an export has since marked delivered or refused are also split out.
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from datetime import datetime

import certifi
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import get_settings
from app.services.dm_seen import signal_report

DEFAULT_CHANNELS = ("TryAlgoViz", "DreamScenicAi")


async def main(since: datetime, channels: list[str]) -> None:
    settings = get_settings()
    db = AsyncIOMotorClient(settings.MONGODB_URI, tlsCAFile=certifi.where())[settings.MONGODB_DB_NAME]
    for channel_id in channels:
        seen: dict[str, list[datetime]] = defaultdict(list)
        async for row in db.dm_seen.find({"channel_id": channel_id, "seen_at": {"$gte": since}}):
            seen[str(row["peer_id"])].append(row["seen_at"])
        acted: dict[str, list[datetime]] = defaultdict(list)
        async for row in db.dm_messages.find(
            {"channel_id": channel_id, "from_me": False, "created_at": {"$gte": since}}, {"peer_id": 1, "created_at": 1}
        ):
            acted[str(row["peer_id"])].append(row["created_at"])
        by_delivery: dict[str, list[tuple[str, datetime]]] = defaultdict(list)
        async for event in db.comment_dm_events.find(
            {"channel_id": channel_id, "status": "sent", "sent_at": {"$gte": since}},
            {"from_id": 1, "sent_at": 1, "delivery": 1},
        ):
            row = (str(event.get("from_id")), event["sent_at"])
            by_delivery["all"].append(row)
            if event.get("delivery"):
                by_delivery[f"export says {event['delivery']}"].append(row)
        receipts = sum(len(v) for v in seen.values())
        print(f"== {channel_id} since {since.isoformat()} ({receipts} read receipts kept)")
        for label, rows in sorted(by_delivery.items()):
            report = signal_report(rows, seen, acted)
            share = 100 * report["any_sign"] / report["sent"] if report["sent"] else 0
            print(f"   {label}: {report} -> {share:.0f}% show a sign")


if __name__ == "__main__":
    asyncio.run(main(datetime.fromisoformat(sys.argv[1]), sys.argv[2:] or list(DEFAULT_CHANNELS)))
