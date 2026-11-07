"""A city comment rule (@themapworld): read the city in a comment, DM its map, tally the ask."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from typing import Any

import pytest
import requests

from app.routers.instagram_dm_setup import city_answers
from app.services import city_tally, meet_city
from app.services import comment_dm_store as store
from app.services.comment_dm import (
    MAX_ATTEMPTS,
    SKIP_ALREADY_MESSAGED,
    SKIP_OWN,
    SKIP_TALLY_ONLY,
    CommentEvent,
    DmRule,
    stored_rule,
)
from app.services.comment_dm_worker import send_one
from app.services.meet_city import (
    CityMap,
    PlaceRead,
    answer_for_place,
    answer_for_read,
    city_in,
    comment_dm,
    quick_city,
)
from app.timezone import IST
from tests.mongo_fakes import DB

NOW = datetime(2026, 11, 4, 20, 0, tzinfo=IST)
CHANNEL = {
    "channel_id": "DreamScenicAi",
    "platform": "instagram",
    "instagram_user_id": "me",
    "instagram_username": "themapworld",
}
TMW = CityMap(utm_prefix="tmw")
RULE = DmRule(city_map=TMW, reply_texts=["Sent, check your DMs 📩"])


@pytest.mark.parametrize(
    ("comment", "slug"),
    [
        ("London 🔥", "london"),
        ("Mumbai please!!", "mumbai"),
        ("nyc!!", "new-york"),
        ("do Bombay next", "mumbai"),
        ("San Francisco plsss", "san-francisco"),
        ("where's Toronto", "toronto"),
        ("where's Lyon", None),
        ("nice", None),
        ("🔥🔥", None),
    ],
)
def test_a_comment_naming_one_of_our_cities_is_read_without_the_model(comment: str, slug: str | None) -> None:
    assert city_in(comment) == slug


def test_a_place_the_model_reads_is_ours_or_on_our_list() -> None:
    assert answer_for_place("Bombay").slug == "mumbai"
    lagos = answer_for_place("Lagos")
    assert (lagos.outcome, lagos.name) == ("unlisted", "Lagos")
    assert answer_for_place("são paulo").name == "São Paulo"
    assert answer_for_place("").outcome == "ignored"


def test_a_neighbourhood_is_its_city_and_tallies_under_it() -> None:
    battersea = answer_for_read(PlaceRead(place="Battersea", city="London"))
    assert (battersea.outcome, battersea.slug, battersea.name, battersea.normalised) == (
        "matched",
        "london",
        "London",
        "battersea",
    )
    assert answer_for_read(PlaceRead(place="Bandra", city="Mumbai")).slug == "mumbai"
    # A neighbourhood of a city we don't have names the city, not the neighbourhood.
    ikeja = answer_for_read(PlaceRead(place="Ikeja", city="Lagos"))
    assert (ikeja.outcome, ikeja.name, ikeja.normalised) == ("unlisted", "Lagos", "ikeja")
    # A country has no city: it is named as itself.
    assert answer_for_read(PlaceRead(place="Nigeria", city=None)).name == "Nigeria"


def test_a_place_mentioned_in_passing_gets_nothing() -> None:
    passing = PlaceRead(place="Battersea", city="London", asks=False)
    assert answer_for_read(passing).outcome == "ignored"


@pytest.mark.parametrize(
    "comment",
    ["London", "London 🔥", "Mumbai please!!", "do Delhi next", "my city Pune", "San Francisco plsss", "NYC pls 🙏"],
)
def test_the_city_alone_or_plainly_asked_for_needs_no_model(comment: str) -> None:
    assert quick_city(comment) is not None


@pytest.mark.parametrize(
    "comment", ["Love London", "London rocks", "getting to London is a nightmare tho", "Paris was better"]
)
def test_a_city_in_any_other_comment_goes_to_the_model(comment: str) -> None:
    assert quick_city(comment) is None


def _all_text(message: Any) -> str:
    return json.dumps(message.model_dump(mode="json"), ensure_ascii=False)


def test_our_city_gets_its_map_as_a_card_with_a_follow_button() -> None:
    message = comment_dm(answer_for_place("London"), TMW)
    assert message is not None
    [card] = message.cards
    assert card.title == "Your London map is ready 🗺️"
    assert card.link == (
        "https://tryalgoviz.com/algorithms/graph/city-map/meet/?city=london&utm_source=instagram&utm_medium=dm"
        "&utm_campaign=comment-dm&utm_content=tmw-london"
    )
    assert [(b.type, b.title) for b in card.buttons] == [("url", "📍 Open the map"), ("profile", "🔔 Follow for more")]


def test_another_place_is_on_our_list_with_the_general_map() -> None:
    message = comment_dm(answer_for_place("Lagos"), TMW)
    assert message is not None
    assert message.text == "Lagos is on our list ✅, we're adding it. Try the cities that are live meanwhile 👇"
    assert str(message.buttons[0].url).endswith("utm_content=tmw-other") and "city=" not in str(message.buttons[0].url)


def test_the_copy_never_names_the_site_and_has_no_em_dash() -> None:
    for place in ("London", "Lagos", "New York"):
        message = comment_dm(answer_for_place(place), TMW)
        assert message is not None
        # Only the link may point at the site; no text says its name.
        words = [message.text] + [c.title + c.subtitle for c in message.cards]
        words += [b.title for b in message.buttons] + [b.title for c in message.cards for b in c.buttons]
        assert not any("tryalgoviz" in w.lower() for w in words)
        assert "—" not in _all_text(message) and "–" not in _all_text(message)


def test_a_city_rule_needs_no_dm_text_and_an_ordinary_rule_is_unchanged() -> None:
    assert RULE.message().cards[0].title == "Your London map is ready 🗺️"
    plain = DmRule(dm_text="Here's the link!")
    assert plain.city_map is None and plain.message().text == "Here's the link!"
    with pytest.raises(ValueError):
        DmRule()  # still: an ordinary rule must say something


# --- the queue and the sender ---


def _db() -> DB:
    stored = stored_rule(RULE, None, NOW - timedelta(hours=1)).model_dump()
    return DB(channels=[{**CHANNEL, store.DEFAULT_FIELD: stored}])


def _comment(text: str, cid: str = "c1", who: str = "u1") -> CommentEvent:
    return CommentEvent(comment_id=cid, media_id="181", text=text, username=f"@{who}", from_id=who, created_at=NOW)


async def _enqueue(db: DB, event: CommentEvent) -> str:
    return await store.enqueue(db, CHANNEL, event, source="webhook", now=NOW, rng=random.Random(0))


class FakeGemini:
    """Answers by comment: a place name (a city that asks), a PlaceRead, None, or an error to raise."""

    def __init__(self, answers: dict[str, str | PlaceRead | None | Exception]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    async def extract_place(self, comment_text: str) -> PlaceRead | None:
        self.asked.append(comment_text)
        answer = self.answers[comment_text]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, str):
            return PlaceRead(place=answer, city=answer)
        return answer


class FakeService:
    provider = "instagram"

    def __init__(self) -> None:
        self.dms: list[dict[str, Any]] = []
        self.replies: list[str] = []

    def send_private_reply(self, ig_user_id: str, comment_id: str, message: dict[str, Any]) -> str:
        self.dms.append(message)
        return f"m{len(self.dms)}"

    def reply_to_comment(self, comment_id: str, message: str) -> str:
        self.replies.append(message)
        return "r1"


class FakeManager:
    def __init__(self, service: FakeService) -> None:
        self.service = service

    async def get_service_and_user_id(self, channel_id: str, prefer: str | None = None):
        return self.service, "ig1"


async def _send(db: DB, gemini: FakeGemini | None, service: FakeService, cid: str = "c1") -> str:
    event = next(e for e in db.comment_dm_events.docs if e["comment_id"] == cid)
    return await send_one(db, FakeManager(service), dict(event), NOW, gemini)


def _event_doc(db: DB, cid: str = "c1") -> dict[str, Any]:
    return next(e for e in db.comment_dm_events.docs if e["comment_id"] == cid)


@pytest.mark.asyncio
class TestCityComments:
    async def test_a_named_city_is_queued_with_its_card_and_tallied_at_once(self) -> None:
        db = _db()
        assert await _enqueue(db, _comment("London 🔥")) == store.QUEUED
        doc = _event_doc(db)
        assert doc["cards"][0]["link"].endswith("utm_content=tmw-london") and doc["city_check"] == "done"
        [row] = db.dm_city_answers.docs
        assert (row["channel_id"], row["source"], row["slug"], row["message_id"]) == (
            "DreamScenicAi",
            "comment",
            "london",
            "comment:c1",
        )
        service = FakeService()
        assert await _send(db, FakeGemini({}), service) == "sent"
        assert service.replies == ["Sent, check your DMs 📩"] and len(service.dms) == 1

    async def test_a_comment_with_no_letters_never_queues(self) -> None:
        db = _db()
        assert await _enqueue(db, _comment("🔥🔥")) == store.NO_CITY
        assert db.comment_dm_events.docs == [] and db.dm_city_answers.docs == []

    async def test_another_place_is_read_by_the_model_then_dmed_and_tallied(self) -> None:
        db = _db()
        assert await _enqueue(db, _comment("do Lagos next")) == store.QUEUED
        assert _event_doc(db)["city_check"] == "pending" and db.dm_city_answers.docs == []
        service = FakeService()
        assert await _send(db, FakeGemini({"do Lagos next": "Lagos"}), service) == "sent"
        [dm] = service.dms
        assert "Lagos is on our list ✅" in json.dumps(dm, ensure_ascii=False)
        assert service.replies == ["Sent, check your DMs 📩"]
        [row] = db.dm_city_answers.docs
        assert (row["outcome"], row["normalised"], row["replied"]) == ("unlisted", "lagos", True)

    async def test_no_place_means_no_dm_and_no_public_reply(self) -> None:
        db = _db()
        await _enqueue(db, _comment("so satisfying"))
        service = FakeService()
        assert await _send(db, FakeGemini({"so satisfying": None}), service) == "skipped"
        assert service.dms == [] and service.replies == []
        doc = _event_doc(db)
        assert (doc["status"], doc["skip_reason"]) == ("skipped", store.NO_CITY)
        assert db.dm_city_answers.docs == []

    async def test_a_failed_model_call_is_retried_then_given_up_on(self) -> None:
        db = _db()
        await _enqueue(db, _comment("where's Toronto"))
        service = FakeService()
        broken = FakeGemini({"where's Toronto": RuntimeError("gateway down")})
        assert await _send(db, broken, service) == "pending"
        doc = _event_doc(db)
        assert doc["send_after"] > NOW and doc["city_attempts"] == 1
        db.comment_dm_events.docs[0]["city_attempts"] = MAX_ATTEMPTS - 1
        assert await _send(db, broken, service) == "skipped" and service.dms == []

    async def test_a_comment_with_no_city_does_not_block_the_next_one(self) -> None:
        db = _db()
        assert await _enqueue(db, _comment("nice", cid="c1")) == store.QUEUED
        # Still waiting to be read for a city: "London" a moment later is not "already messaged".
        assert await _enqueue(db, _comment("London", cid="c2")) == store.QUEUED

    async def test_two_places_from_one_person_on_one_reel_get_one_dm(self) -> None:
        db = _db()
        # Both wait on the model, so neither blocks the other at the queue; the sender gives them one DM.
        assert await _enqueue(db, _comment("do Lagos next", cid="c1")) == store.QUEUED
        assert await _enqueue(db, _comment("and Accra", cid="c2")) == store.QUEUED
        gemini = FakeGemini({"do Lagos next": "Lagos", "and Accra": "Accra"})
        service = FakeService()
        assert await _send(db, gemini, service, "c1") == "sent"
        assert await _send(db, gemini, service, "c2") == "skipped"
        assert _event_doc(db, "c2")["skip_reason"] == SKIP_ALREADY_MESSAGED and len(service.dms) == 1
        # Both asks still count as demand.
        assert sorted(r["normalised"] for r in db.dm_city_answers.docs) == ["accra", "lagos"]

    async def test_a_second_named_city_is_already_messaged_but_still_tallied(self) -> None:
        db = _db()
        await _enqueue(db, _comment("London", cid="c1"))
        assert await _enqueue(db, _comment("Paris", cid="c2")) == SKIP_ALREADY_MESSAGED
        assert sorted(r["slug"] for r in db.dm_city_answers.docs) == ["london", "paris"]

    async def test_a_repeat_ask_the_model_reads_is_tallied_but_never_dmed_twice(self) -> None:
        db = _db()
        await _enqueue(db, _comment("London", cid="c1"))
        # "taipei" from someone already messaged on this reel was once skipped unread, and its ask lost.
        assert await _enqueue(db, _comment("taipei", cid="c2")) == store.QUEUED
        service = FakeService()
        assert await _send(db, FakeGemini({"taipei": "Taipei"}), service, "c2") == "skipped"
        assert service.dms == [] and _event_doc(db, "c2")["skip_reason"] == SKIP_ALREADY_MESSAGED
        assert sorted(r["normalised"] for r in db.dm_city_answers.docs) == ["london", "taipei"]
        # The first comment still gets its DM.
        assert await _send(db, None, service, "c1") == "sent" and len(service.dms) == 1

    async def test_a_neighbourhood_gets_its_citys_card_and_counts_for_the_city(self) -> None:
        db = _db()
        comment = "Battersea please, I live there"
        await _enqueue(db, _comment(comment))
        service = FakeService()
        read = PlaceRead(place="Battersea", city="London")
        assert await _send(db, FakeGemini({comment: read}), service) == "sent"
        assert "Your London map is ready" in json.dumps(service.dms[0], ensure_ascii=False)
        [row] = db.dm_city_answers.docs
        assert (row["slug"], row["name"], row["place"], row["normalised"]) == (
            "london",
            "London",
            "Battersea",
            "battersea",
        )

    async def test_a_place_only_mentioned_gets_no_dm_and_no_reply(self) -> None:
        db = _db()
        comment = "getting to battersea is a nightmare tho"
        assert await _enqueue(db, _comment(comment)) == store.QUEUED
        service = FakeService()
        read = PlaceRead(place="Battersea", city="London", asks=False)
        assert await _send(db, FakeGemini({comment: read}), service) == "skipped"
        assert service.dms == [] and service.replies == [] and db.dm_city_answers.docs == []
        assert _event_doc(db)["skip_reason"] == store.NO_ASK

    async def test_the_model_is_never_asked_about_a_city_we_matched(self) -> None:
        db = _db()
        await _enqueue(db, _comment("Mumbai please!!"))
        gemini = FakeGemini({})
        assert await _send(db, gemini, FakeService()) == "sent" and gemini.asked == []

    async def test_an_ordinary_rule_still_sends_its_own_dm(self) -> None:
        stored = stored_rule(DmRule(dm_text="hi"), None, NOW - timedelta(hours=1)).model_dump()
        db = DB(channels=[{**CHANNEL, store.DEFAULT_FIELD: stored}])
        assert await _enqueue(db, _comment("London")) == store.QUEUED
        doc = _event_doc(db)
        assert doc["dm_text"] == "hi" and "city_check" not in doc and db.dm_city_answers.docs == []
        assert meet_city.TYPED_ANSWER_CHANNEL == "TryAlgoViz"


# --- tally only: a city vote, nothing sent (@themapworld, 2026-11-07) ---

TALLY = DmRule(city_map=TMW, reply_texts=["Sent, check your DMs 📩"], tally_only=True)


def _tally_db(rule: DmRule = TALLY) -> DB:
    stored = stored_rule(rule, None, NOW - timedelta(hours=1)).model_dump()
    return DB(channels=[{**CHANNEL, store.DEFAULT_FIELD: stored}])


class FakeLikes:
    """Graph's multi-id like read: refuses the whole batch (code 100) when any id in it is gone."""

    def __init__(self, likes: dict[str, int], fail_code: int | None = None) -> None:
        self.likes = likes
        self.fail_code = fail_code
        self.calls: list[list[str]] = []

    def get_comment_like_counts(self, comment_ids: list[str]) -> dict[str, int]:
        self.calls.append(list(comment_ids))
        code = self.fail_code if self.fail_code is not None else (100 if set(comment_ids) - set(self.likes) else None)
        if code is not None:
            response = requests.Response()
            response.status_code = 400
            response._content = json.dumps({"error": {"code": code}}).encode()
            raise requests.HTTPError(response=response)
        return {cid: self.likes[cid] for cid in comment_ids}


def test_a_tally_only_rule_needs_nothing_to_send() -> None:
    rule = DmRule(tally_only=True)
    assert rule.reads_cities and rule.city_map is None


@pytest.mark.asyncio
class TestTallyOnly:
    async def test_a_named_city_is_tallied_and_never_sent_or_replied_to(self) -> None:
        db = _tally_db()
        assert await _enqueue(db, _comment("London 🔥")) == SKIP_TALLY_ONLY
        doc = _event_doc(db)
        assert (doc["status"], doc["reply_text"], doc["cards"]) == ("skipped", None, [])
        [row] = db.dm_city_answers.docs
        assert (row["slug"], row["replied"]) == ("london", False)

    async def test_a_place_the_model_reads_is_tallied_and_never_sent(self) -> None:
        db = _tally_db()
        assert await _enqueue(db, _comment("do Lagos next")) == store.QUEUED
        service = FakeService()
        assert await _send(db, FakeGemini({"do Lagos next": "Lagos"}), service) == "skipped"
        assert service.dms == [] and service.replies == []
        assert _event_doc(db)["skip_reason"] == SKIP_TALLY_ONLY
        [row] = db.dm_city_answers.docs
        assert (row["normalised"], row["replied"]) == ("lagos", False)

    async def test_every_vote_counts_but_our_own(self) -> None:
        db = _tally_db()
        before_rule = _comment("Paris", cid="c1").model_copy(update={"created_at": NOW - timedelta(days=3)})
        assert await _enqueue(db, before_rule) == SKIP_TALLY_ONLY
        # A second comment from the same person on the same reel is another vote.
        assert await _enqueue(db, _comment("London", cid="c2", who="u1")) == SKIP_TALLY_ONLY
        assert await _enqueue(db, _comment("Tokyo", cid="c3", who="me")) == SKIP_OWN
        assert sorted(r["slug"] for r in db.dm_city_answers.docs) == ["london", "paris"]

    async def test_without_a_city_map_cities_are_still_read(self) -> None:
        db = _tally_db(DmRule(tally_only=True))
        assert await _enqueue(db, _comment("Mumbai please!!")) == SKIP_TALLY_ONLY
        assert await _enqueue(db, _comment("🔥🔥", cid="c2")) == store.NO_CITY
        assert await _enqueue(db, _comment("do Lagos next", cid="c3")) == store.QUEUED
        assert await _send(db, FakeGemini({"do Lagos next": "Lagos"}), FakeService(), "c3") == "skipped"
        assert sorted(r["normalised"] for r in db.dm_city_answers.docs) == ["lagos", "mumbai please"]

    async def test_a_queued_tally_event_never_sends_whatever_its_state(self) -> None:
        db = _tally_db()
        await _enqueue(db, _comment("do Lagos next"))
        db.comment_dm_events.docs[0]["city_check"] = "done"  # as if read elsewhere
        service = FakeService()
        assert await _send(db, None, service) == "skipped" and service.dms == []


@pytest.mark.asyncio
class TestCityLikes:
    async def _rows(self, *ids: str) -> DB:
        db = _tally_db()
        for i, cid in enumerate(ids):
            await _enqueue(db, _comment("London", cid=cid, who=f"u{i}"))
        return db

    async def test_likes_are_read_onto_each_row_then_not_again_for_a_while(self) -> None:
        db = await self._rows("c1", "c2")
        reader = FakeLikes({"c1": 7, "c2": 0})
        assert await city_tally.refresh_likes(db, reader, "DreamScenicAi", NOW) == 2
        assert {r["message_id"]: r["like_count"] for r in db.dm_city_answers.docs} == {
            "comment:c1": 7,
            "comment:c2": 0,
        }
        assert await city_tally.refresh_likes(db, reader, "DreamScenicAi", NOW + timedelta(minutes=5)) == 0
        assert await city_tally.refresh_likes(db, reader, "DreamScenicAi", NOW + timedelta(minutes=31)) == 2

    async def test_a_deleted_comment_is_marked_gone_and_the_rest_still_read(self) -> None:
        db = await self._rows("c1", "c2")
        reader = FakeLikes({"c1": 3})  # c2 deleted
        assert await city_tally.refresh_likes(db, reader, "DreamScenicAi", NOW) == 1
        rows = {r["message_id"]: r for r in db.dm_city_answers.docs}
        assert rows["comment:c1"]["like_count"] == 3 and rows["comment:c2"]["comment_gone"] is True
        reader.calls.clear()
        await city_tally.refresh_likes(db, reader, "DreamScenicAi", NOW + timedelta(hours=1))
        assert reader.calls == [["c1"]]

    async def test_a_rate_limit_marks_nothing_gone(self) -> None:
        db = await self._rows("c1")
        with pytest.raises(requests.HTTPError):
            await city_tally.refresh_likes(db, FakeLikes({}, fail_code=4), "DreamScenicAi", NOW)
        assert "comment_gone" not in db.dm_city_answers.docs[0]

    async def test_the_tally_sums_likes_and_filters_to_one_reel(self) -> None:
        db = await self._rows("c1", "c2")
        await _enqueue(db, CommentEvent(comment_id="c9", media_id="999", text="Paris", username="z", created_at=NOW))
        await city_tally.refresh_likes(db, FakeLikes({"c1": 7, "c2": 2, "c9": 50}), "DreamScenicAi", NOW)
        out = await city_answers("DreamScenicAi", media_id="181", db=db)
        [london] = out.demand
        assert (london.slug, london.count, london.likes, london.top_likes) == ("london", 2, 9, 7)
        assert {r.like_count for r in out.recent} == {7, 2}
        everything = await city_answers("DreamScenicAi", media_id=None, db=db)
        assert {d.slug: d.likes for d in everything.demand} == {"london": 9, "paris": 50}
