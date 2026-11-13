"""Read receipts: kept once per message, our own reads ignored, and the comment-DM signal counted."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.services import dm_seen
from tests.mongo_fakes import DB

T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
MS = int(T0.timestamp() * 1000)


def read(sender: str, mid: str, account: str = "acct", stamp: int = MS) -> dict:
    return {
        "object": "instagram",
        "entry": [
            {
                "id": account,
                "time": stamp,
                "messaging": [
                    {"sender": {"id": sender}, "recipient": {"id": account}, "timestamp": stamp, "read": {"mid": mid}}
                ],
            }
        ],
    }


def test_parses_reads_and_ignores_messages():
    payload = read("p1", "m1")
    payload["entry"][0]["messaging"].append(
        {"sender": {"id": "p2"}, "recipient": {"id": "acct"}, "timestamp": MS, "message": {"mid": "m2", "text": "hi"}}
    )
    assert dm_seen.parse_seen_webhook(payload) == [dm_seen.SeenEvent("acct", "p1", "m1", T0)]
    assert dm_seen.parse_seen_webhook({"object": "page", "entry": payload["entry"]}) == []


def test_records_once_and_skips_our_own_reads_and_unknown_accounts():
    db = DB(channels=[{"channel_id": "TryAlgoViz", "platform": "instagram", "instagram_user_id": "acct"}])
    assert asyncio.run(dm_seen.record_webhook(db, read("p1", "m1"), T0)) == 1
    assert asyncio.run(dm_seen.record_webhook(db, read("p1", "m1", stamp=MS + 5000), T0)) == 0
    assert asyncio.run(dm_seen.record_webhook(db, read("acct", "m9"), T0)) == 0  # we read their message
    assert asyncio.run(dm_seen.record_webhook(db, read("p3", "m3", account="other"), T0)) == 0
    (row,) = db.dm_seen.docs
    assert (row["channel_id"], row["peer_id"], row["message_id"], row["seen_at"]) == ("TryAlgoViz", "p1", "m1", T0)


def test_signal_report_counts_any_sign_after_the_dm():
    sent = [("a", T0), ("b", T0), ("c", T0), ("d", T0)]
    seen = {"a": [T0 + timedelta(minutes=5)], "d": [T0 - timedelta(days=1)]}  # d's read was of an older message
    acted = {"b": [T0 + timedelta(hours=1)], "a": [T0 + timedelta(minutes=6)]}
    assert dm_seen.signal_report(sent, seen, acted) == {"sent": 4, "seen": 1, "acted": 2, "any_sign": 2, "no_sign": 2}
