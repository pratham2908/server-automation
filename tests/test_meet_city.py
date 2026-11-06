"""A city typed in the meet DM flow: read, matched to the meet tool's cities, answered once, tallied."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import instagram_dm_setup as setup_router
from app.services import dm_replies, meet_city, meet_city_answers
from app.services.dm_content import DmButton, DmMessage
from app.services.meet_city import read_answer, typed_reply
from tests.conftest import GLOBAL_API_KEY
from tests.mongo_fakes import DB

NOW = datetime(2026, 11, 4, 13, 40, tzinfo=UTC)
ME = "17841400000000001"
THEM = "1884954306000181"
CHANNEL = {"channel_id": "TryAlgoViz", "platform": "instagram", "instagram_user_id": ME}
PROMPT = "Which city is your group in? 👇"
OTHER_PROMPT = "Which city is your group in? Type it right here 👇\n\nOpen the map and look for it."


@pytest.mark.parametrize(
    ("text", "outcome", "slug", "name"),
    [
        ("bangalore", "matched", "bengaluru", "Bengaluru"),
        ("BLR", "matched", "bengaluru", "Bengaluru"),
        ("Bengalūru", "matched", "bengaluru", "Bengaluru"),
        ("I am from Greater Noida", "matched", "noida", "Noida"),
        ("gurgaon", "matched", "gurugram", "Gurugram"),
        ("Ghaziabad", "matched", "delhi", "Delhi"),
        ("Pune city", "matched", "pune", "Pune"),
        ("New-York", "matched", "new-york", "New York"),
        ("newyork", "matched", "new-york", "New York"),
        ("sf", "matched", "san-francisco", "San Francisco"),
        ("is pune there?", "matched", "pune", "Pune"),
        ("Madras", "matched", "chennai", "Chennai"),
        ("Jaipur", "matched", "jaipur", "Jaipur"),
        ("Trivandrum", "matched", "thiruvananthapuram", "Thiruvananthapuram"),
        ("Hisar", "unlisted", None, "Hisar"),
        ("İstanbul", "matched", "istanbul", "Istanbul"),
        ("Kraków", "unlisted", None, "Kraków"),
        ("patna", "unlisted", None, "Patna"),
        ("cochin", "matched", "kochi", "Kochi"),
        ("Lahore??", "unlisted", None, "Lahore"),
        ("Ahmedabad??", "matched", "ahmedabad", "Ahmedabad"),
        ("Thane district", "unlisted", None, "Thane"),
        ("Kozhikode, Kerala", "unlisted", None, "Kozhikode"),
        ("thanks", "ignored", None, ""),
        ("Other", "ignored", None, ""),
        ("what is this?", "ignored", None, ""),
        ("send link pls", "ignored", None, ""),
        ("👍", "ignored", None, ""),
        ("we are four friends and we live very far apart", "ignored", None, ""),
    ],
)
def test_a_typed_reply_is_one_of_our_cities_another_place_or_not_a_place(
    text: str, outcome: str, slug: str | None, name: str
) -> None:
    answer = read_answer(text)
    assert (answer.outcome, answer.slug, answer.name) == (outcome, slug, name)


def test_istanbul_tallies_without_its_dotted_capital() -> None:
    assert read_answer("İstanbul").normalised == "istanbul" == read_answer("istanbul").normalised


def test_a_city_without_a_chip_gets_the_chip_template_and_a_typed_link() -> None:
    reply = typed_reply(read_answer("jaipur"))
    assert reply is not None and reply.text.startswith("Jaipur it is 🗺️\n\nThe map is already set to Jaipur.")
    [button] = reply.buttons
    assert button.title == "🗺️ Open the map"
    assert button.url == (
        "https://tryalgoviz.com/algorithms/graph/city-map/meet/?city=jaipur&utm_source=instagram&utm_medium=dm"
        "&utm_campaign=comment-dm&utm_content=meet-typed-jaipur"
    )


def test_another_place_is_on_our_list_never_missing() -> None:
    reply = typed_reply(read_answer("Hisar"))
    assert reply is not None
    assert reply.text == (
        "Hisar is on our list ✅ We're adding it soon, and the most-asked cities get their own reel 🙌"
    )
    assert "city=" not in str(reply.buttons[0].url) and str(reply.buttons[0].url).endswith(
        "utm_content=meet-typed-other"
    )
    assert typed_reply(read_answer("thanks")) is None


# --- the webhook path ---

CHIP_DELHI = DmMessage(
    text="Delhi it is 🏛️\n\nThe map is already set to Delhi.",
    buttons=[DmButton(type="url", title="🗺️ Open the map", url=meet_city.map_url("meet-chip-delhi", "delhi"))],
)


def _typed(text: str, mid: str = "m1", at: datetime = NOW, chip: bool = False) -> dict[str, Any]:
    message: dict[str, Any] = {"mid": mid, "text": text}
    if chip:
        message["quick_reply"] = {"payload": "dmr:x"}
    stamp = int(at.timestamp() * 1000)
    return {
        "object": "instagram",
        "entry": [{"id": ME, "time": stamp, "messaging": [{"sender": {"id": THEM}, "recipient": {"id": ME},
                                                         "timestamp": stamp, "message": message}]}],
    }  # fmt: skip


def _ours(text: str, ago: timedelta, mid: str) -> dict[str, Any]:
    return {"message_id": mid, "channel_id": "TryAlgoViz", "peer_id": THEM, "from_me": True, "text": text,
            "kind": "text", "created_at": NOW - ago}  # fmt: skip


async def _db(*ours: dict[str, Any]) -> DB:
    db = DB(channels=[dict(CHANNEL)], dm_messages=list(ours))
    await dm_replies.register(db, "TryAlgoViz", [CHIP_DELHI], NOW - timedelta(days=1))
    return db


@pytest.mark.asyncio
class TestAnswers:
    async def test_a_chip_city_typed_sends_exactly_what_its_chip_sends(self) -> None:
        db = await _db(_ours(PROMPT, timedelta(seconds=20), "p1"))
        assert await meet_city_answers.record_webhook(db, _typed("new delhi"), NOW) == 1
        [owed] = db.dm_outbox.docs
        assert DmMessage.model_validate(owed["message"]) == CHIP_DELHI
        assert (owed["recipient_id"], owed["reason"], owed["check_follow"]) == (THEM, "typed_city", False)
        [row] = db.dm_city_answers.docs
        assert (row["slug"], row["outcome"], row["reply_kind"], row["prompt_id"]) == ("delhi", "matched", "chip", "p1")

    async def test_a_city_typed_after_the_other_reply_is_answered_and_tallied(self) -> None:
        db = await _db(_ours(PROMPT, timedelta(minutes=2), "p1"), _ours(OTHER_PROMPT, timedelta(minutes=1), "p2"))
        assert await meet_city_answers.record_webhook(db, _typed("Kraków"), NOW) == 1
        [owed] = db.dm_outbox.docs
        assert owed["message"]["text"].startswith("Kraków is on our list ✅")
        [row] = db.dm_city_answers.docs
        assert (row["normalised"], row["outcome"], row["text"], row["prompt_id"]) == (
            "krakow",
            "unlisted",
            "Kraków",
            "p2",
        )

    async def test_a_redelivered_message_and_a_second_answer_send_nothing_more(self) -> None:
        db = await _db(_ours(PROMPT, timedelta(seconds=20), "p1"))
        assert await meet_city_answers.record_webhook(db, _typed("Trivandrum"), NOW) == 1
        assert await meet_city_answers.record_webhook(db, _typed("Trivandrum"), NOW) == 0
        assert await meet_city_answers.record_webhook(db, _typed("Kerala", mid="m2"), NOW) == 0
        assert len(db.dm_outbox.docs) == 1 and len(db.dm_city_answers.docs) == 1

    async def test_not_a_city_is_logged_unanswered_and_a_real_city_after_it_still_is(self) -> None:
        db = await _db(_ours(PROMPT, timedelta(seconds=20), "p1"))
        assert await meet_city_answers.record_webhook(db, _typed("thanks"), NOW) == 0
        assert await meet_city_answers.record_webhook(db, _typed("Kozhikode", mid="m2"), NOW) == 1
        assert [(r["outcome"], r["replied"]) for r in db.dm_city_answers.docs] == [
            ("ignored", False),
            ("unlisted", True),
        ]

    async def test_only_a_reply_to_the_question_counts(self) -> None:
        # Too late: the question was asked 31 minutes ago.
        late = await _db(_ours(PROMPT, timedelta(minutes=31), "p1"))
        assert await meet_city_answers.record_webhook(late, _typed("Trivandrum"), NOW) == 0
        # We've said something else since: the chip's reply went out, so "Pune" is about something else.
        moved_on = await _db(
            _ours(PROMPT, timedelta(minutes=2), "p1"), _ours("Delhi it is", timedelta(minutes=1), "r1")
        )
        assert await meet_city_answers.record_webhook(moved_on, _typed("Pune"), NOW) == 0
        # A chip tap is answered by its own reply, never as a typed city.
        tapped = await _db(_ours(PROMPT, timedelta(seconds=20), "p1"))
        assert await meet_city_answers.record_webhook(tapped, _typed("Delhi", chip=True), NOW) == 0
        # Another channel's DMs are none of this flow's business.
        other = DB(
            channels=[{**CHANNEL, "channel_id": "zara kapoor"}],
            dm_messages=[{**_ours(PROMPT, timedelta(seconds=20), "p1"), "channel_id": "zara kapoor"}],
        )
        assert await meet_city_answers.record_webhook(other, _typed("Trivandrum"), NOW) == 0
        for db in (late, moved_on, tapped, other):
            assert db.dm_outbox.docs == [] and db.dm_city_answers.docs == []

    async def test_a_chip_city_whose_chip_reply_is_gone_falls_back_to_the_template(self) -> None:
        db = DB(channels=[dict(CHANNEL)], dm_messages=[_ours(PROMPT, timedelta(seconds=20), "p1")])
        assert await meet_city_answers.record_webhook(db, _typed("Mumbai"), NOW) == 1
        [owed] = db.dm_outbox.docs
        assert owed["message"]["buttons"][0]["url"].endswith("utm_content=meet-typed-mumbai")
        assert db.dm_city_answers.docs[0]["reply_kind"] == "typed"


def test_the_tally_groups_by_city_most_asked_first() -> None:
    def row(mid: str, peer: str, text: str) -> dict[str, Any]:
        a = read_answer(text)
        return {"message_id": mid, "channel_id": "TryAlgoViz", "peer_id": peer, "text": text, "name": a.name,
                "normalised": a.normalised, "slug": a.slug, "outcome": a.outcome, "replied": a.outcome != "ignored",
                "reply_kind": "typed", "answered_at": NOW, "created_at": NOW + timedelta(seconds=int(mid[1:]))}  # fmt: skip

    db = DB(
        channels=[dict(CHANNEL)],
        dm_city_answers=[row("m1", "a", "Kraków"), row("m2", "b", "krakow"), row("m3", "c", "bangalore"),
                         row("m4", "d", "BLR"), row("m5", "d", "Bengaluru"), row("m6", "e", "Hisar"),
                         row("m7", "f", "thanks")],
    )  # fmt: skip
    app.dependency_overrides[setup_router.get_db] = lambda: db
    try:
        body = (
            TestClient(app)
            .get("/api/v1/channels/TryAlgoViz/instagram/dm-setup/city-answers", headers={"x-api-key": GLOBAL_API_KEY})
            .json()
        )
    finally:
        app.dependency_overrides.pop(setup_router.get_db, None)
    assert [(d["name"], d["people"], d["count"]) for d in body["demand"]] == [
        ("Bengaluru", 2, 3),
        ("Krakow", 2, 2),
        ("Hisar", 1, 1),
    ]
    assert len(body["recent"]) == 7 and body["recent"][0]["text"] == "thanks"
