"""The DM store: webhook messages kept once, a catch-up that reads only what changed, and the numbers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests

from app.services import dm_store
from app.services.dm_stats import dm_stats
from app.timezone import IST
from tests.mongo_fakes import DB

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=IST)
OUR_ID = "17841425918010637"
CHANNEL = {
    "channel_id": "zk",
    "platform": "instagram",
    "instagram_username": "zarakapoor._",
    "instagram_webhook_id": OUR_ID,
}


def _hook(sender: str, recipient: str, mid: str, text: str = "hi", at: datetime = NOW, **message: Any) -> dict:
    item = {
        "sender": {"id": sender},
        "recipient": {"id": recipient},
        "timestamp": int(at.timestamp() * 1000),
        "message": {"mid": mid, "text": text, **message},
    }
    return {"object": "instagram", "entry": [{"id": OUR_ID, "time": int(at.timestamp() * 1000), "messaging": [item]}]}


# --- webhook -----------------------------------------------------------------------------------


class TestParse:
    def test_reads_a_message_and_ignores_reads_and_comments(self):
        payload = _hook("777", OUR_ID, "mid.1", "hello")
        payload["entry"][0]["messaging"].append({"sender": {"id": "777"}, "read": {"mid": "mid.0"}})
        payload["entry"].append({"id": OUR_ID, "time": 1, "changes": [{"field": "comments", "value": {}}]})
        [event] = dm_store.parse_message_webhook(payload)
        assert (event.sender_id, event.message_id, event.text) == ("777", "mid.1", "hello")
        assert event.created_at == NOW

    @pytest.mark.parametrize(
        ("message", "kind", "link"),
        [
            ({"attachments": [{"type": "ig_reel", "payload": {"url": "https://x/r"}}]}, "share", "https://x/r"),
            ({"attachments": [{"type": "image", "payload": {"url": "https://x/i"}}]}, "attachment", "https://x/i"),
            ({"reply_to": {"story": {"url": "https://x/s", "id": "1"}}}, "story", "https://x/s"),
            ({"is_unsupported": True}, "unsupported", None),
        ],
    )
    def test_kinds(self, message: dict, kind: str, link: str | None):
        [event] = dm_store.parse_message_webhook(_hook("777", OUR_ID, "m", **message))
        assert (event.kind, event.link) == (kind, link)


@pytest.mark.asyncio
class TestRecord:
    async def test_theirs_and_our_echo_land_in_one_conversation_once(self):
        db = DB(channels=[dict(CHANNEL)])
        assert await dm_store.record_webhook(db, _hook("777", OUR_ID, "mid.1"), NOW) == 1
        assert await dm_store.record_webhook(db, _hook(OUR_ID, "777", "mid.2", "yo", is_echo=True), NOW) == 1
        assert await dm_store.record_webhook(db, _hook("777", OUR_ID, "mid.1"), NOW) == 0
        assert [(m["peer_id"], m["from_me"]) for m in db.dm_messages.docs] == [("777", False), ("777", True)]
        assert len(db.dm_conversations.docs) == 1

    async def test_a_new_person_after_the_backfill_is_a_whole_conversation(self):
        db = DB(channels=[{**CHANNEL, dm_store.BACKFILLED_FIELD: NOW}])
        await dm_store.record_webhook(db, _hook("777", OUR_ID, "mid.1"), NOW)
        assert db.dm_conversations.docs[0]["history"] == "complete"
        before = DB(channels=[dict(CHANNEL)])
        await dm_store.record_webhook(before, _hook("777", OUR_ID, "mid.1"), NOW)
        assert before.dm_conversations.docs[0]["history"] is None

    async def test_the_same_message_under_another_id_is_not_kept_twice(self):
        db = DB(channels=[dict(CHANNEL)])
        await dm_store.record_webhook(db, _hook("777", OUR_ID, "mid.1", "hey"), NOW)
        await dm_store.record_webhook(db, _hook("777", OUR_ID, "other-id", "hey", at=NOW + timedelta(seconds=2)), NOW)
        assert len(db.dm_messages.docs) == 1

    async def test_an_unsent_message_is_marked_deleted(self):
        db = DB(channels=[dict(CHANNEL)])
        await dm_store.record_webhook(db, _hook("777", OUR_ID, "mid.1"), NOW)
        await dm_store.record_webhook(db, _hook("777", OUR_ID, "mid.1", is_deleted=True), NOW)
        assert db.dm_messages.docs[0]["deleted"] is True

    async def test_an_unknown_account_is_ignored(self):
        db = DB(channels=[dict(CHANNEL)])
        payload = _hook("777", "999", "mid.1")
        payload["entry"][0]["id"] = "999"
        assert await dm_store.record_webhook(db, payload, NOW) == 0


# --- catch-up ----------------------------------------------------------------------------------


def _api_msg(mid: str, sender_id: str, sender: str, at: str, text: str = "hi") -> dict[str, Any]:
    return {"id": mid, "created_time": at, "from": {"id": sender_id, "username": sender}, "message": text}


class FakeReader:
    def __init__(self) -> None:
        self.conversations: list[dict[str, Any]] = []
        self.threads: dict[str, tuple[list[dict[str, Any]], bool]] = {}
        self.reads: list[str] = []
        self.lists = 0
        self.broken: set[str] = set()

    def add(self, cid: str, peer: str, name: str, updated: str, messages: list[dict[str, Any]], more: bool = False):
        self.conversations.insert(
            0,
            {
                "id": cid,
                "updated_time": updated,
                "participants": {"data": [{"id": OUR_ID, "username": "zarakapoor._"}, {"id": peer, "username": name}]},
                "messages": {"data": [{"id": messages[0]["id"]}]},
            },
        )
        self.threads[cid] = (messages, more)

    def list_conversations(self, ig_user_id: str, limit: int, after: str | None) -> dict[str, Any]:
        self.lists += 1
        start = int(after or 0)
        page = self.conversations[start : start + 2]  # small pages so paging is exercised
        more = start + 2 < len(self.conversations)
        return {"data": page, "paging": {"cursors": {"after": str(start + 2)}, "next": "x"} if more else {}}

    def get_conversation_page(self, cid: str) -> tuple[list[dict[str, Any]], bool]:
        self.reads.append(cid)
        if cid in self.broken:
            raise requests.HTTPError("500")
        return self.threads[cid]


async def _catch_up(db: DB, reader: FakeReader) -> dict[str, int]:
    return await dm_store.catch_up(db, db.channels.docs[0], reader, "28507087082286164", NOW, spacing=0)


@pytest.mark.asyncio
class TestCatchUp:
    async def test_backfill_then_only_changed_conversations_are_read(self):
        db = DB(channels=[dict(CHANNEL)])
        reader = FakeReader()
        for i in range(4):
            at = f"2026-11-0{i + 1}T10:00:00+0000"
            reader.add(f"c{i}", f"p{i}", f"fan{i}", at, [_api_msg(f"m{i}", f"p{i}", f"fan{i}", at)], more=i == 0)
        first = await _catch_up(db, reader)
        assert first == {"conversations": 4, "read": 4, "added": 4, "failed": 0}
        assert {c["peer_id"]: c["history"] for c in db.dm_conversations.docs}["p0"] == "partial"
        assert db.channels.docs[0][dm_store.BACKFILLED_FIELD] == NOW
        assert db.channels.docs[0][dm_store.PENDING_FIELD] is False

        # Nothing moved: one list call, stopped at the first conversation, nothing read.
        reader.reads.clear()
        reader.lists = 0
        assert (await _catch_up(db, reader))["read"] == 0 and reader.lists == 1

        # p3 wrote again (newest first now); p1's message arrived by webhook so needs no read.
        new = _api_msg("m9", "p3", "fan3", "2026-11-03T11:00:00+0000", "again")
        reader.conversations[0]["updated_time"] = "2026-11-03T11:00:00+0000"
        reader.conversations[0]["messages"]["data"] = [{"id": "m9"}]
        reader.threads["c3"] = ([new, *reader.threads["c3"][0]], False)
        reader.reads.clear()
        stats = await _catch_up(db, reader)
        assert reader.reads == ["c3"] and stats["added"] == 1

    async def test_a_failed_read_makes_the_next_walk_full(self):
        db = DB(channels=[dict(CHANNEL)])
        reader = FakeReader()
        reader.add(
            "c0", "p0", "fan0", "2026-11-01T10:00:00+0000", [_api_msg("m0", "p0", "fan0", "2026-11-01T10:00:00+0000")]
        )
        reader.broken.add("c0")
        assert (await _catch_up(db, reader))["failed"] == 1
        assert db.channels.docs[0][dm_store.PENDING_FIELD] is True
        assert dm_store.BACKFILLED_FIELD not in db.channels.docs[0]
        reader.broken.clear()
        assert (await _catch_up(db, reader))["added"] == 1

    async def test_our_own_messages_are_from_me(self):
        db = DB(channels=[dict(CHANNEL)])
        reader = FakeReader()
        at = "2026-11-01T10:00:00+0000"
        reader.add(
            "c0", "p0", "fan0", at, [_api_msg("m1", OUR_ID, "zarakapoor._", at), _api_msg("m0", "p0", "fan0", at)]
        )
        await _catch_up(db, reader)
        assert sorted((m["message_id"], m["from_me"]) for m in db.dm_messages.docs) == [("m0", False), ("m1", True)]


# --- numbers -----------------------------------------------------------------------------------


def _m(mid: str, peer: str, mine: bool, ago_hours: float) -> dict[str, Any]:
    return {
        "message_id": mid,
        "peer_id": peer,
        "from_me": mine,
        "created_at": NOW.astimezone(UTC) - timedelta(hours=ago_hours),
    }


def test_numbers():
    conversations = [
        {"peer_id": "a", "history": "complete"},  # they started 2 days ago; we answered 30 min later
        {"peer_id": "b", "history": "complete"},  # comment-to-DM opened it; they answered
        {"peer_id": "c", "history": "complete"},  # we opened it by hand; no answer
        {"peer_id": "d", "history": "partial"},  # old and long: who started it is unknown; waiting on us
        {"peer_id": "e", "history": "complete"},  # they wrote 40 days ago, nothing since
    ]
    messages = [
        _m("a1", "a", False, 48),
        _m("a2", "a", True, 47.5),
        _m("b1", "b", True, 10),
        _m("b2", "b", False, 9),
        _m("c1", "c", True, 5),
        _m("d1", "d", False, 2),
        _m("e1", "e", False, 960),
    ]
    stats = dm_stats(conversations, messages, comment_dm_ids={"b1"}, commenter_ids={"b"}, now=NOW, days=7)
    assert stats["totals"] == {
        "conversations": 4,
        "accounts_messaged": 3,
        "messages_in": 3,
        "messages_out": 3,
        "new_by_them": 1,
        "new_by_us": 1,
        "new_by_comment_dm": 1,
        "reply_rate": 1.0,
        "median_reply_minutes": 30.0,
        "comment_dm_reply_rate": 1.0,
        "from_commenters": 1,
    }
    assert stats["started_all_time"] == {"them": 2, "us": 1, "comment_dm": 1, "unknown": 1}
    # b's last word was theirs too: waiting on us are b, d and e; b and d still inside 24 h.
    assert (stats["awaiting_reply"], stats["awaiting_reply_open"]) == (3, 2)
    assert len(stats["series"]) == 7 and sum(d["started_by_them"] for d in stats["series"]) == 1
