"""A reel of ours shared into our DMs gets that reel's comment-to-DM message, once per person per reel."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from app.services import dm_outbox, dm_reel_share
from app.services.comment_dm import DmRule
from app.services.comment_dm_store import save_default, save_reel_rule
from app.services.meet_city import CityMap
from app.timezone import IST
from tests.mongo_fakes import DB

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=IST)
ME = "17841400000000001"
FAN = "1002933425502276"
CHANNEL = {"channel_id": "ch", "platform": "instagram", "instagram_user_id": ME, "instagram_username": "tryalgoviz"}
MEET_RULE = DmRule(
    triggers=["meet"],
    dm_text="Here you go! Pick your city 📍",
    buttons=[{"title": "📍 Find our spot", "url": "https://tryalgoviz.com/maps/meet/"}],
)


class Graph:
    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    def get_permalinks(self, media_ids: list[str]) -> dict[str, str]:
        self.asked.append(list(media_ids))
        codes = {"m1": "DfTqWG9EtP3", "m2": "DfRFdNBk9tX"}
        return {m: f"https://www.instagram.com/reel/{codes[m]}/" for m in media_ids if m in codes}


class Manager:
    def __init__(self) -> None:
        self.graph = Graph()

    async def get_service_and_user_id(self, channel_id: str, prefer: str | None = None):
        return self.graph, ME


def share(mid: str, link: str, *, peer: str = FAN, at: datetime = NOW, **over: Any) -> dict[str, Any]:
    return {
        "message_id": mid,
        "channel_id": "ch",
        "peer_id": peer,
        "from_me": False,
        "kind": "share",
        "text": "",
        "link": link,
        "created_at": at - timedelta(minutes=1),
        **over,
    }


LINK = "https://www.instagram.com/reel/DfTqWG9EtP3/"


async def _db(*messages: dict[str, Any], rule: DmRule | None = MEET_RULE) -> DB:
    db = DB(
        channels=[dict(CHANNEL)],
        videos=[
            {"channel_id": "ch", "video_id": "v1", "instagram_media_id": "m1"},
            {"channel_id": "ch", "video_id": "v2", "instagram_media_id": "m2"},
        ],
        dm_messages=list(messages),
        dm_conversations=[{"channel_id": "ch", "peer_id": FAN, "peer_username": "Rutuja.k"}],
    )
    if rule is not None:
        await save_reel_rule(db, "ch", "m1", rule, NOW)
    return db


def _outcome(db: DB, mid: str) -> str:
    row = next(m for m in db.dm_messages.docs if m["message_id"] == mid)
    return str(row[dm_reel_share.FIELD]["outcome"])


async def test_our_shared_reel_gets_its_comment_dm_queued_once() -> None:
    db = await _db(share("s1", LINK), share("s2", LINK + "?igsh=abc"))
    assert await dm_reel_share.answer_shared_reels(db, Manager(), NOW) == 1
    (row,) = db.dm_outbox.docs
    assert row["recipient_id"] == FAN and row["reason"] == dm_reel_share.REASON
    assert row["message"]["text"] == "Here you go! Pick your city 📍"
    assert row["message"]["buttons"][0]["url"] == "https://tryalgoviz.com/maps/meet/"
    assert (_outcome(db, "s1"), _outcome(db, "s2")) == (dm_reel_share.QUEUED, dm_reel_share.ALREADY)
    # Looked at once: a second tick does nothing.
    assert await dm_reel_share.answer_shared_reels(db, Manager(), NOW) == 0
    assert len(db.dm_outbox.docs) == 1


async def test_permalinks_are_read_once_and_kept() -> None:
    db = await _db(share("s1", LINK))
    first = Manager()
    await dm_reel_share.answer_shared_reels(db, first, NOW)
    db.dm_messages.docs.append(share("s3", LINK, peer="555"))
    second = Manager()
    await dm_reel_share.answer_shared_reels(db, second, NOW)
    assert first.graph.asked and second.graph.asked == []
    assert len(db.dm_outbox.docs) == 2


async def test_someone_who_got_it_from_a_comment_is_not_sent_it_again() -> None:
    db = await _db(share("s1", LINK))
    db.comment_dm_events.docs.append(
        {"channel_id": "ch", "media_id": "m1", "person": "rutuja.k", "from_id": "other-id", "status": "sent"}
    )
    await dm_reel_share.answer_shared_reels(db, Manager(), NOW)
    assert db.dm_outbox.docs == [] and _outcome(db, "s1") == dm_reel_share.ALREADY


async def test_reels_without_a_rule_others_reels_and_city_rules_send_nothing() -> None:
    db = await _db(
        share("noRule", "https://www.instagram.com/reel/DfRFdNBk9tX/"),
        share("notOurs", "https://www.instagram.com/reel/SomeoneElse1/"),
        share("noLink", ""),
    )
    await dm_reel_share.answer_shared_reels(db, Manager(), NOW)
    assert db.dm_outbox.docs == []
    assert _outcome(db, "noRule") == dm_reel_share.NO_RULE
    assert _outcome(db, "notOurs") == dm_reel_share.NOT_OURS
    assert _outcome(db, "noLink") == dm_reel_share.NOT_A_REEL

    city = await _db(share("c1", LINK), rule=None)
    await save_default(city, "ch", DmRule(city_map=CityMap()), NOW)
    await dm_reel_share.answer_shared_reels(city, Manager(), NOW)
    assert city.dm_outbox.docs == [] and _outcome(city, "c1") == dm_reel_share.NEEDS_COMMENT


async def test_a_switched_off_rule_sends_nothing() -> None:
    db = await _db(share("s1", LINK), rule=MEET_RULE.model_copy(update={"enabled": False}))
    await dm_reel_share.answer_shared_reels(db, Manager(), NOW)
    assert db.dm_outbox.docs == [] and _outcome(db, "s1") == dm_reel_share.NO_RULE


async def test_our_own_shares_and_old_ones_are_left_alone() -> None:
    old = share("old", LINK, at=NOW - timedelta(hours=24))
    ours = share("ours", LINK, from_me=True)
    db = await _db(old, ours)
    await dm_reel_share.answer_shared_reels(db, Manager(), NOW)
    assert db.dm_outbox.docs == []
    assert all(dm_reel_share.FIELD not in m for m in db.dm_messages.docs)


async def test_the_queued_dm_is_sent_by_the_outbox() -> None:
    db = await _db(share("s1", LINK))
    await dm_reel_share.answer_shared_reels(db, Manager(), NOW)

    class Sender:
        provider = "instagram"

        def __init__(self) -> None:
            self.sent: list[tuple[str, dict[str, Any]]] = []

        def send_message(self, ig_user_id: str, recipient_id: str, message: dict[str, Any]) -> str:
            self.sent.append((recipient_id, message))
            return "m1"

    sender = Sender()

    class SendManager:
        async def get_service_and_user_id(self, channel_id: str, prefer: str | None = None):
            return sender, ME

    assert await dm_outbox.send_due(db, SendManager(), lambda: NOW) == 1
    assert sender.sent and sender.sent[0][0] == FAN
