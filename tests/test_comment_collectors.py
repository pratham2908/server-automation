from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.services import comment_collector_store as store
from app.services import comment_collector_worker as worker
from app.services import comment_collectors as cc
from tests.mongo_fakes import DB

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


def make(**over: Any) -> cc.Collector:
    fields: dict[str, Any] = {
        "name": "City requests",
        "question": "Which city does this commenter want us to map next?",
        "id": "city",
        "channel_id": "map",
        "active_from": NOW - timedelta(days=1),
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(over)
    return cc.Collector(**fields)


def comment(cid: str, text: str, **over: Any) -> dict[str, Any]:
    row = {
        "comment_id": cid,
        "channel_id": "map",
        "media_id": "reel1",
        "author": f"user_{cid}",
        "username": f"user_{cid}",
        "text": text,
        "own": False,
        "commented_at": NOW - timedelta(hours=1),
    }
    row.update(over)
    return row


class Reader:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def collect_answers(self, prompt: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class TestDefinition:
    def test_fixed_choices_need_two(self):
        with pytest.raises(ValidationError):
            cc.CollectorIn(name="Next sort", question="Which sort next?", answer_type="choices", choices=["Bubble"])

    def test_choices_are_cleaned_and_deduplicated(self):
        c = cc.CollectorIn(
            name="n", question="Which?", answer_type="choices", choices=[" Bubble  sort", "bubble sort", "Quick"]
        )
        assert c.choices == ["Bubble sort", "Quick"]

    def test_property_scope_needs_values(self):
        with pytest.raises(ValidationError):
            cc.AppliesTo(mode="property", property_id="format")

    def test_applies_by_scope(self):
        assert cc.applies(make(), "any", None)
        assert not cc.applies(make(enabled=False), "any", None)
        reels = make(applies_to=cc.AppliesTo(mode="reels", media_ids=["r1"]))
        assert cc.applies(reels, "r1", None) and not cc.applies(reels, "r2", None)
        prop = make(applies_to=cc.AppliesTo(mode="property", property_id="fmt", value_ids=["vote"]))
        assert cc.applies(prop, "r", {"fmt": ["vote", "x"]})
        assert not cc.applies(prop, "r", {"fmt": ["listicle"]})


class TestPromptAndAnswers:
    def test_one_prompt_asks_every_collector(self):
        algo = make(id="algo", question="Which algorithm next?", answer_type="choices", choices=["Bubble", "Quick"])
        prompt = cc.build_prompt("do tokyo!", [make(guidance="A neighbourhood counts as its city."), algo])
        assert '"q1": Which city' in prompt and '"q2": Which algorithm' in prompt
        assert "neighbourhood counts" in prompt and '["Bubble", "Quick"]' in prompt
        assert '"do tokyo!"' in prompt

    def test_fixed_choices_keep_only_their_choices(self):
        algo = make(id="algo", question="Which?", answer_type="choices", choices=["Bubble sort", "Quick sort"])
        assert cc.answers_for(algo, ["quick SORT", "Heap sort"]) == ["Quick sort"]

    def test_one_answer_unless_several_are_allowed(self):
        assert cc.answers_for(make(), ["Paris", "Rome"]) == ["Paris"]
        assert cc.answers_for(make(multiple=True), ["Paris", "rome", "paris"]) == ["Paris", "rome"]

    def test_merges_apply_to_new_answers(self):
        assert cc.answers_for(make(merges={"nyc": "New York"}), ["NYC"]) == ["New York"]

    def test_missing_or_odd_keys_answer_nothing(self):
        out = cc.parse_answers({"q1": "Lagos", "q9": ["x"]}, [make(), make(id="other")])
        assert out == {"city": ["Lagos"], "other": []}


class TestCounting:
    def test_a_person_counts_once_per_reel(self):
        rows = [
            {"answer": "Tokyo", "author": "a", "media_id": "r1", "comment_id": "1", "like_count": 4},
            {"answer": "tokyo", "author": "a", "media_id": "r1", "comment_id": "2", "like_count": 1},
            {"answer": "Tokyo", "author": "a", "media_id": "r2", "comment_id": "3"},
            {"answer": "Lagos", "author": "b", "media_id": "r1", "comment_id": "4", "like_count": 30},
        ]
        tokyo, lagos = cc.tally(rows)
        assert (tokyo["answer"], tokyo["votes"], tokyo["people"], tokyo["comments"], tokyo["likes"]) == (
            "Tokyo",
            2,
            1,
            3,
            5,
        )
        assert (lagos["votes"], lagos["likes"]) == (1, 30)

    def test_likes_are_read_at_a_few_ages_then_left(self):
        def due(age: timedelta, read_at_age: timedelta | None) -> bool:
            written = NOW - age
            return cc.likes_due(written, None if read_at_age is None else written + read_at_age, NOW)

        assert not due(timedelta(hours=2), None)  # too young: the first read is at 6 hours
        assert due(timedelta(hours=7), None)
        assert not due(timedelta(hours=7), timedelta(hours=6, minutes=30))  # read past the 6h mark already
        assert due(timedelta(days=2), timedelta(hours=7))  # passed the 1-day mark since
        assert not due(timedelta(days=2), timedelta(days=1, hours=1))
        assert due(timedelta(days=8), timedelta(days=3, hours=2))  # the 7-day read
        assert due(timedelta(days=14, hours=3), timedelta(days=7, hours=1))  # the last read
        assert not due(timedelta(days=16), timedelta(days=7))  # past the window: left alone

    def test_five_reads_over_a_comments_life(self):
        written = NOW - timedelta(days=20)
        read_at = None
        reads = 0
        t = written
        while t < NOW:
            if cc.likes_due(written, read_at, t):
                read_at, reads = t, reads + 1
            t += timedelta(minutes=10)
        assert reads == 5

    def test_naive_mongo_times_compare(self):
        naive = (NOW - timedelta(hours=8)).replace(tzinfo=None)
        assert cc.likes_due(naive, None, NOW)


def _db_with(collectors: list[cc.Collector], comments: list[dict[str, Any]], **more: Any) -> DB:
    return DB(comment_collectors=[c.model_dump() for c in collectors], comment_log=comments, **more)


class TestWorker:
    def test_reads_each_comment_once_and_counts_its_answer(self):
        db = _db_with([make()], [comment("1", "do Battersea pls"), comment("2", "love it")])
        reader = Reader([{"q1": ["London"]}, {"q1": []}])
        assert run(worker.read_channel(db, reader, "map", NOW)) == 1
        rows = db.collector_answers.docs
        assert [(r["answer"], r["comment_id"], r["author"]) for r in rows] == [("London", "1", "user_1")]
        # Both comments are marked read: nothing is asked again.
        assert run(worker.read_channel(db, Reader([]), "map", NOW)) == 0
        assert len(reader.prompts) == 2

    def test_own_and_older_comments_are_not_read(self):
        old = comment("1", "Tokyo", commented_at=NOW - timedelta(days=3))
        mine = comment("2", "Tokyo", own=True)
        db = _db_with([make()], [old, mine])
        run(worker.read_channel(db, Reader([]), "map", NOW))
        assert db.collector_answers.docs == []

    def test_a_collector_for_other_reels_asks_nothing(self):
        only = make(applies_to=cc.AppliesTo(mode="reels", media_ids=["reel9"]))
        db = _db_with([only], [comment("1", "Tokyo")])
        reader = Reader([])
        run(worker.read_channel(db, reader, "map", NOW))
        assert reader.prompts == []
        assert "city" in db.comment_log.docs[0]["collected"]

    def test_property_scope_reads_the_reels_tags(self):
        prop = make(applies_to=cc.AppliesTo(mode="property", property_id="fmt", value_ids=["vote"]))
        db = _db_with(
            [prop],
            [comment("1", "Tokyo"), comment("2", "Paris", media_id="reel2")],
            videos=[
                {"channel_id": "map", "instagram_media_id": "reel1", "properties": {"fmt": ["vote"]}},
                {"channel_id": "map", "instagram_media_id": "reel2", "properties": {"fmt": ["listicle"]}},
            ],
        )
        reader = Reader([{"q1": ["Tokyo"]}])
        run(worker.read_channel(db, reader, "map", NOW))
        assert [r["answer"] for r in db.collector_answers.docs] == ["Tokyo"]
        assert len(reader.prompts) == 1

    def test_a_failing_model_is_retried_then_given_up_on(self):
        db = _db_with([make()], [comment("1", "Tokyo")])
        boom = RuntimeError("model down")
        for _ in range(3):
            run(worker.read_channel(db, Reader([boom]), "map", NOW))
        row = db.comment_log.docs[0]
        assert row["collect_attempts"] == 3 and row["collect_failed"] is True
        assert run(worker.read_channel(db, Reader([]), "map", NOW)) == 0

    def test_a_new_collector_still_reads_comments_another_has_read(self):
        db = _db_with([make()], [comment("1", "Tokyo, and the sorting one")])
        run(worker.read_channel(db, Reader([{"q1": ["Tokyo"]}]), "map", NOW))
        algo = make(id="algo", name="Algo", question="Which algorithm?")
        db.comment_collectors.docs.append(algo.model_dump())
        reader = Reader([{"q1": ["Sorting"]}])
        run(worker.read_channel(db, reader, "map", NOW))
        assert "Which algorithm?" in reader.prompts[0] and "Which city" not in reader.prompts[0]
        assert sorted(r["answer"] for r in db.collector_answers.docs) == ["Sorting", "Tokyo"]


class _Likes:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        self.asked: list[list[str]] = []

    def get_comment_like_counts(self, ids: list[str]) -> dict[str, int]:
        self.asked.append(ids)
        return {i: self.counts.get(i, 0) for i in ids}


class TestStore:
    def test_merge_rewrites_counted_answers_and_drops_double_votes(self):
        c = make(multiple=True)
        db = _db_with([c], [])
        rows = [
            store.answer_row(c, comment("1", "nyc"), "NYC", NOW),
            store.answer_row(c, comment("2", "new york or nyc"), "NYC", NOW),
            store.answer_row(c, comment("2", "new york or nyc"), "New York", NOW),
        ]
        run(store.insert_rows(db, rows))
        merged = run(store.merge_answers(db, c, ["NYC"], "New York", NOW))
        assert merged.merges == {"nyc": "New York"}
        assert sorted((r["comment_id"], r["answer"]) for r in db.collector_answers.docs) == [
            ("1", "New York"),
            ("2", "New York"),
        ]

    def test_rows_are_kept_once(self):
        c = make()
        db = _db_with([c], [])
        row = store.answer_row(c, comment("1", "Tokyo"), "Tokyo", NOW)
        assert run(store.insert_rows(db, [row, dict(row)])) == 1

    def test_likes_are_read_only_when_due(self):
        c = make()
        db = _db_with([c], [])
        eight_hours = NOW - timedelta(hours=8)
        fresh = store.answer_row(
            c, comment("1", "Tokyo", commented_at=eight_hours), "Tokyo", NOW, likes_at=NOW - timedelta(hours=1)
        )
        stale = store.answer_row(
            c, comment("2", "Lagos", commented_at=eight_hours), "Lagos", NOW, likes_at=NOW - timedelta(hours=6)
        )
        run(store.insert_rows(db, [fresh, stale]))
        reader = _Likes({"2": 7})
        assert run(store.refresh_likes(db, reader, "map", NOW)) == 1
        assert reader.asked == [["2"]]
        assert [r.get("like_count") for r in db.collector_answers.docs] == [None, 7]

    def test_city_votes_are_copied_with_their_likes(self):
        c = make()
        db = _db_with(
            [c],
            [comment("1", "battersea")],
            dm_city_answers=[
                {
                    "channel_id": "map",
                    "source": "comment",
                    "message_id": "comment:1",
                    "media_id": "reel1",
                    "username": "ana",
                    "peer_id": "p1",
                    "text": "battersea",
                    "name": "London",
                    "outcome": "matched",
                    "like_count": 12,
                    "answered_at": NOW - timedelta(hours=3),
                },
                {
                    "channel_id": "map",
                    "source": "comment",
                    "message_id": "comment:2",
                    "name": "x",
                    "outcome": "ignored",
                },
                {"channel_id": "map", "source": "dm", "message_id": "m1", "name": "Paris", "outcome": "matched"},
            ],
        )
        assert run(store.import_city_votes(db, c, NOW)) == 1
        (row,) = db.collector_answers.docs
        assert (row["answer"], row["like_count"], row["author"]) == ("London", 12, "ana")
        assert "city" in db.comment_log.docs[0]["collected"]
        assert run(store.import_city_votes(db, c, NOW)) == 0


@pytest.fixture
def client():
    from app import main
    from app.database import get_db
    from app.dependencies import verify_api_key

    db = DB(
        channels=[{"channel_id": "map"}],
        videos=[{"channel_id": "map", "instagram_media_id": "reel1", "title": "Tokyo vote"}],
    )
    main.app.dependency_overrides[get_db] = lambda: db
    main.app.dependency_overrides[verify_api_key] = lambda: None
    yield TestClient(main.app), db
    main.app.dependency_overrides.pop(get_db, None)
    main.app.dependency_overrides.pop(verify_api_key, None)


class TestRoutes:
    def test_create_count_and_list(self, client):
        http, db = client
        made = http.post(
            "/api/v1/channels/map/collectors",
            json={"name": "City requests", "question": "Which city next?", "read_back_days": 2},
        )
        assert made.status_code == 201, made.text
        c = cc.Collector.model_validate(db.comment_collectors.docs[0])
        run(store.insert_rows(db, [store.answer_row(c, comment("1", "Tokyo"), "Tokyo", NOW, like_count=3)]))
        tally = http.get(f"/api/v1/channels/map/collectors/{c.id}/tally").json()
        assert tally["entries"][0]["answer"] == "Tokyo" and tally["entries"][0]["likes"] == 3
        assert tally["reels"] == [{"media_id": "reel1", "title": "Tokyo vote", "published_at": None}]
        listed = http.get("/api/v1/channels/map/collectors").json()
        assert listed[0]["comments_counted"] == 1
        who = http.get(f"/api/v1/channels/map/collectors/{c.id}/answers", params={"answer": "tokyo"}).json()
        assert [w["comment_id"] for w in who] == ["1"]

    def test_bad_definitions_and_unknown_collectors_are_refused(self, client):
        http, _ = client
        bad = http.post(
            "/api/v1/channels/map/collectors", json={"name": "x", "question": "Which?", "answer_type": "choices"}
        )
        assert bad.status_code == 422
        assert http.get("/api/v1/channels/map/collectors/nope/tally").status_code == 404
        assert (
            http.post("/api/v1/channels/nochannel/collectors", json={"name": "x", "question": "Which?"}).status_code
            == 404
        )


def _graph_error(code: int, transient: bool = False):
    import json as _json

    import requests

    response = requests.Response()
    response.status_code = 400
    response._content = _json.dumps({"error": {"code": code, "is_transient": transient}}).encode()
    return requests.HTTPError("400", response=response)


class _PickyLikes:
    """Refuses any batch over 2 ids as Graph does a too-big one, and one id as deleted."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def get_comment_like_counts(self, ids: list[str]) -> dict[str, int]:
        self.calls.append(list(ids))
        if len(ids) > 2:
            raise _graph_error(2, transient=True)
        if "gone" in ids:
            raise _graph_error(100)
        return {i: int(i) for i in ids}


class TestLikeBatches:
    def test_a_batch_refused_as_too_big_is_read_in_halves(self):
        from app.services.city_tally import read_like_counts

        likes, gone = run(read_like_counts(_PickyLikes(), ["1", "2", "3", "4", "5"]))
        assert likes == {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5} and gone == []

    def test_a_deleted_comment_doesnt_hide_the_rest(self):
        from app.services.city_tally import read_like_counts

        likes, gone = run(read_like_counts(_PickyLikes(), ["1", "gone"]))
        assert likes == {"1": 1} and gone == ["gone"]

    def test_batches_stay_under_what_graph_refuses(self):
        from app.services.city_tally import LIKES_PER_CALL

        assert LIKES_PER_CALL <= 40


class TestReelContext:
    def test_the_prompt_names_the_reel(self):
        prompt = cc.build_prompt("chatelet", [make()], "4 Friends Across Paris: Where Should They Meet? 📍\n#maps")
        assert '"4 Friends Across Paris: Where Should They Meet? 📍"' in prompt and "#maps" not in prompt
        assert "reel's own question" in prompt

    def test_the_worker_passes_the_reels_title(self):
        db = _db_with(
            [make()],
            [comment("1", "chatelet")],
            videos=[{"channel_id": "map", "instagram_media_id": "reel1", "title": "Where should they meet in Paris?"}],
        )
        reader = Reader([{"q1": []}])
        run(worker.read_channel(db, reader, "map", NOW))
        assert "Where should they meet in Paris?" in reader.prompts[0]


class TestAnswerOrder:
    def test_most_liked_first_unread_last(self):
        c = make()
        db = _db_with([c], [])
        rows = [
            store.answer_row(c, comment("1", "Tokyo"), "Tokyo", NOW, like_count=2),
            store.answer_row(c, comment("2", "tokyo pls"), "Tokyo", NOW),
            store.answer_row(c, comment("3", "TOKYO"), "Tokyo", NOW, like_count=40),
            store.answer_row(c, comment("4", "do tokyo"), "Tokyo", NOW, like_count=0),
        ]
        run(store.insert_rows(db, rows))
        liked = run(store.answers_for(db, c.id, answer="tokyo", sort="likes"))
        assert [r["comment_id"] for r in liked] == ["3", "1", "4", "2"]


class _SilentLikes:
    """Like Graph: a batch leaves a deleted comment out; asked alone, it doesn't exist."""

    def get_comment_like_counts(self, ids: list[str]) -> dict[str, int]:
        if ids == ["deleted"]:
            raise _graph_error(100)
        return {i: 3 for i in ids if i != "deleted"}


def test_a_comment_left_out_of_a_batch_is_marked_gone():
    from app.services.city_tally import read_like_counts

    likes, gone = run(read_like_counts(_SilentLikes(), ["a", "deleted", "b"]))
    assert likes == {"a": 3, "b": 3} and gone == ["deleted"]


class TestKinds:
    def test_the_kind_is_asked_with_the_collectors_in_one_prompt(self):
        prompt = cc.build_prompt("@sam look 😭", [make()], "Tokyo meet", caption="Tokyo meet. Who wins?", ask_kind=True)
        assert '"kind": What kind of comment' in prompt and '"tag"' in prompt and '"q1": Which city' in prompt
        assert 'The reel\'s caption: "Who wins?"' in prompt

    def test_unknown_kinds_are_other(self):
        assert cc.parse_kind({"kind": "Humour"}) == "humour"
        assert cc.parse_kind({"kind": "sarcasm"}) == "other"
        assert cc.parse_kind({}) == "other"

    def _db(self, **more: Any) -> DB:
        return _db_with(
            [],
            [comment("1", "@sam look"), comment("2", "bro 😭", commented_at=NOW - timedelta(days=3))],
            channels=[{"channel_id": "map", "comment_kinds": {"enabled": True, "since": NOW - timedelta(days=1)}}],
            videos=[
                {"channel_id": "map", "instagram_media_id": "reel1", "title": "Tokyo", "description": "Tokyo meet"}
            ],
            **more,
        )

    def test_comments_are_sorted_into_kinds_since_the_switch(self):
        db = self._db()
        reader = Reader([{"kind": "tag"}])
        run(worker.read_channel(db, reader, "map", NOW))
        kinds = {r["comment_id"]: r.get("kind") for r in db.comment_log.docs}
        assert kinds == {"1": "tag", "2": None}  # the older one was written before the read-back window
        assert '"Tokyo"' in reader.prompts[0]
        run(worker.read_channel(db, Reader([]), "map", NOW))  # read once

    def test_one_call_answers_both_kind_and_collector(self):
        db = self._db()
        db.comment_collectors.docs.append(make().model_dump())
        reader = Reader([{"kind": "request", "q1": ["Tokyo"]}])
        db.comment_log.docs[1]["commented_at"] = NOW - timedelta(days=5)
        run(worker.read_channel(db, reader, "map", NOW))
        assert len(reader.prompts) == 1
        assert db.comment_log.docs[0]["kind"] == "request"
        assert [r["answer"] for r in db.collector_answers.docs] == ["Tokyo"]

    def test_switching_on_with_a_read_back(self):
        db = DB(channels=[{"channel_id": "map"}])
        out = run(store.set_kinds(db, "map", True, 14, NOW))
        assert out["since"] == NOW - timedelta(days=14)
        assert run(store.kinds_since(db, "map")) == NOW - timedelta(days=14)
        run(store.set_kinds(db, "map", False, 0, NOW))
        assert run(store.kinds_since(db, "map")) is None
        assert run(store.channels_to_read(db)) == []

    def test_the_mix_and_most_liked(self):
        rows = [
            comment("1", "lol", kind="humour", like_count=40),
            comment("2", "haha", kind="humour", like_count=3),
            comment("3", "do Rome", kind="request", like_count=None),
            comment("4", "mine", kind="humour", own=True),
        ]
        db = DB(comment_log=rows)
        summary = run(store.kinds_summary(db, "map"))
        humour = next(k for k in summary["kinds"] if k["kind"] == "humour")
        assert summary["total"] == 3 and humour["comments"] == 2 and humour["likes"] == 43
        assert humour["share"] == round(2 / 3, 4) and humour["top"]["text"] == "lol"
        assert summary["reels"] == [{"media_id": "reel1", "total": 3, "mix": {"humour": 2, "request": 1}}]
        liked = run(store.kind_comments(db, "map", kind="humour"))
        assert [r["comment_id"] for r in liked] == ["1", "2"]

    def test_kind_comments_get_their_likes_read(self):
        written = NOW - timedelta(hours=8)
        db = DB(comment_log=[comment("1", "lol", kind="humour", commented_at=written)])
        reader = _Likes({"1": 9})
        assert run(store.refresh_likes(db, reader, "map", NOW)) == 1
        assert db.comment_log.docs[0]["like_count"] == 9


def test_kind_routes(client):
    http, db = client
    on = http.put("/api/v1/channels/map/comment-kinds/settings", json={"enabled": True, "read_back_days": 14})
    assert on.status_code == 200 and on.json()["enabled"] is True and len(on.json()["kinds"]) == 9
    db.comment_log.docs.append(comment("1", "lol", kind="humour", like_count=5))
    summary = http.get("/api/v1/channels/map/comment-kinds").json()
    assert summary["total"] == 1 and summary["reels"][0]["title"] == "Tokyo vote"
    assert (
        http.get("/api/v1/channels/map/comment-kinds/comments", params={"kind": "humour"}).json()[0]["like_count"] == 5
    )
    assert http.get("/api/v1/channels/map/comment-kinds/comments", params={"kind": "nope"}).status_code == 422
