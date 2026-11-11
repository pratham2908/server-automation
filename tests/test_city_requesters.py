"""A city meet reel goes up: everyone who asked for that city gets one reply, once, spaced, stopping on errors."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests

from app.services import city_requester_store as store
from app.services import city_requesters as cr
from tests.mongo_fakes import DB

NOW = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
CH = "DreamScenicAi"


def run(coro):
    return asyncio.run(coro)


def _error(code: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = 400
    response._content = json.dumps({"error": {"code": code, "message": f"code {code}"}}).encode()
    return requests.HTTPError("400", response=response)


class Graph:
    def __init__(self, media: list[dict[str, Any]] | None = None) -> None:
        self.media = media or []
        self.threads: dict[str, list[dict[str, Any]]] = {}
        self.posted: list[tuple[str, str]] = []
        self.fail: dict[str, Exception] = {}  # comment id -> raised on reply
        self.gone: set[str] = set()

    def get_media_page(self, ig_user_id: str, *, limit: int = 25, after: str | None = None) -> dict[str, Any]:
        return {"data": self.media}

    def get_thread(self, comment_id: str, media_id: str = "") -> dict[str, Any]:
        if comment_id in self.gone:
            raise _error(100)
        return {"replies": self.threads.get(comment_id, [])}

    def reply_to_comment(self, comment_id: str, message: str) -> str:
        if comment_id in self.fail:
            raise self.fail[comment_id]
        self.posted.append((comment_id, message))
        self.threads.setdefault(comment_id, []).append(
            {"comment_id": f"r{len(self.posted)}", "author": "themapworld", "text": message}
        )
        return f"r{len(self.posted)}"


def answer(cid: str, city: str, who: str, likes: int = 0, media: str = "old1", hours: int = 1) -> dict[str, Any]:
    return {
        "collector_id": "col",
        "channel_id": CH,
        "comment_id": cid,
        "media_id": media,
        "username": who,
        "text": f"do {city}",
        "answer": city,
        "answer_key": city.lower(),
        "like_count": likes,
        "commented_at": NOW - timedelta(hours=hours),
    }


def log(cid: str, text: str, who: str, media: str = "old1", hours: int = 1, **over: Any) -> dict[str, Any]:
    return {
        "channel_id": CH,
        "comment_id": cid,
        "media_id": media,
        "username": who,
        "text": text,
        "commented_at": NOW - timedelta(hours=hours),
        **over,
    }


def make_db(answers: list[dict[str, Any]], comments: list[dict[str, Any]] | None = None, **more: Any) -> DB:
    return DB(
        channels=[
            {
                "channel_id": CH,
                "instagram_username": "themapworld",
                store.SETTING: {"enabled": True, "since": NOW - timedelta(hours=2)},
            }
        ],
        comment_collectors=[
            {
                "id": "col",
                "channel_id": CH,
                "name": "City requests",
                "question": "Which city?",
                "enabled": True,
                "merges": {"jkt": "Jakarta"},
                "active_from": NOW - timedelta(days=30),
                "created_at": NOW - timedelta(days=30),
                "updated_at": NOW - timedelta(days=30),
            }
        ],
        collector_answers=answers,
        comment_log=comments or [],
        videos=[
            {"channel_id": CH, "instagram_media_id": "old1", "title": "Where Should 4 Friends Meet? #10: Dubai 📍"},
            {"channel_id": CH, "instagram_media_id": "jkt-path", "title": "Jakarta: A* vs Dijkstra"},
        ],
        **more,
    )


MEET = {
    "id": "M15",
    "caption": "Where Should 4 Friends Meet? #16: Jakarta 📍\n\nbody",
    "timestamp": "2026-11-11T07:00:00+0000",
}


async def _no_wait(_: float) -> None:
    return None


def _run_all(db: DB, graph: Graph) -> list[dict[str, Any]]:
    run(store.detect(db, graph, CH, "ig1", NOW))
    out = []
    for job in db.city_requester_jobs.docs:
        if job["status"] == store.JOB_RUNNING:
            out.append(run(store.run_job(db, graph, dict(job), "themapworld", lambda: NOW, _no_wait)))
    return out


class TestRules:
    def test_city_from_headline(self):
        assert cr.headline_city("Where Should 4 Friends Meet? #16: Jakarta 📍\nbody") == "Jakarta"
        assert cr.headline_city("Where Should 4 Friends Meet? #8: Istanbul, Across Two Continents 📍") == "Istanbul"
        assert cr.headline_city("where should 4 friends meet? #15: San Francisco 📍") == "San Francisco"
        assert cr.headline_city("New York, Round 2: Where Should 4 Friends Meet? 📍") is None
        assert cr.headline_city("You voted. Hong Kong won 📍 374 likes") is None
        assert cr.headline_city(None) is None

    def test_texts_rotate_with_no_links(self):
        texts = [cr.reply_text(i, "Jakarta") for i in range(4)]
        assert texts[:3] == [
            "Jakarta is live on our profile now 🗺️ go find your friends' spot!",
            "You asked, we made it 📍 Jakarta just dropped, check our profile",
            "Jakarta's up! Tell us if they picked the right meeting spot 👀",
        ]
        assert texts[3] == texts[0] and not any("http" in t for t in texts)

    def test_one_comment_per_person_most_liked(self):
        rows = [
            {"comment_id": "1", "username": "Ana", "like_count": 1, "commented_at": NOW},
            {"comment_id": "2", "username": "ana", "like_count": 5, "commented_at": NOW - timedelta(hours=3)},
            {"comment_id": "3", "username": "themapworld", "like_count": 9, "commented_at": NOW},
        ]
        assert [(r["username"], r["comment_id"]) for r in cr.one_per_person(rows, ["themapworld"])] == [("ana", "2")]

    def test_already_told_reads_our_replies_naming_the_city(self):
        replies = [
            {"comment_id": "x", "author": "someone", "text": "jakarta is up"},
            {"comment_id": "y", "author": "themapworld", "text": "Jakarta's up! go look"},
        ]
        assert cr.already_told(replies, "themapworld", ["jakarta"]) == "y"
        assert cr.already_told(replies, "themapworld", ["sydney"]) is None


class TestRun:
    def test_everyone_who_asked_gets_one_reply_spaced(self):
        db = make_db(
            [answer("a1", "Jakarta", "ana", likes=3), answer("a2", "Jakarta", "ana"), answer("b1", "jkt", "bo")],
            [
                log("c1", "pls do JAKARTA next", "cy", hours=5),
                log("d1", "jakarta!", "dee", media="M15"),  # on the new reel itself
                log("e1", "I'm in Jakarta", "eve", media="jkt-path"),  # on a Jakarta reel: not a request
                log("f1", "Sydney please", "fay"),
            ],
        )
        graph = Graph([MEET])
        waits: list[float] = []

        async def wait(seconds: float) -> None:
            waits.append(seconds)

        run(store.detect(db, graph, CH, "ig1", NOW))
        (job,) = db.city_requester_jobs.docs
        after = run(store.run_job(db, graph, dict(job), "themapworld", lambda: NOW, wait))
        assert sorted(c for c, _ in graph.posted) == ["a1", "b1", "c1"]
        assert [t for _, t in graph.posted] == [cr.reply_text(i, "Jakarta") for i in range(3)]
        assert waits == [store.SPACING_SECONDS] * 3
        assert after["status"] == store.JOB_DONE and after["sent"] == 3
        assert {r["username"]: r["status"] for r in db.city_requester_replies.docs} == {
            "ana": "sent",
            "bo": "sent",
            "cy": "sent",
        }

    def test_once_ever_per_person_per_city_and_hand_replies_count(self):
        db = make_db([answer("a1", "Jakarta", "ana"), answer("b1", "Jakarta", "bo")])
        graph = Graph([MEET])
        graph.threads["b1"] = [{"comment_id": "hand", "author": "themapworld", "text": "Jakarta is live now!"}]
        _run_all(db, graph)
        assert graph.posted == [("a1", cr.reply_text(0, "Jakarta"))]
        assert {r["username"]: r["status"] for r in db.city_requester_replies.docs}["bo"] == store.TOLD
        # A second Jakarta reel (round 2): nobody hears about Jakarta twice.
        graph.media = [{**MEET, "id": "M20", "caption": "Where Should 4 Friends Meet? #20: Jakarta 📍"}]
        _run_all(db, graph)
        assert len(graph.posted) == 1

    def test_reels_from_before_switching_on_and_other_reels_start_nothing(self):
        db = make_db([answer("a1", "Jakarta", "ana")])
        old = {**MEET, "timestamp": "2026-11-11T04:00:00+0000"}
        other = {"id": "P1", "caption": "Jakarta: A* vs Dijkstra", "timestamp": "2026-11-11T07:30:00+0000"}
        assert run(store.detect(db, Graph([old, other]), CH, "ig1", NOW)) == 0
        db.channels.docs[0][store.SETTING] = {"enabled": False, "since": NOW - timedelta(days=1)}
        assert run(store.detect(db, Graph([MEET]), CH, "ig1", NOW)) == 0

    def test_an_error_stops_the_run_and_a_retry_never_double_posts(self):
        db = make_db([answer("a1", "Jakarta", "ana", hours=3), answer("b1", "Jakarta", "bo", hours=2)])
        graph = Graph([MEET])
        graph.fail["b1"] = _error(4)  # rate limited
        (after,) = _run_all(db, graph)
        assert after["status"] == store.JOB_STOPPED and "bo" in after["error"]
        assert graph.posted == [("a1", cr.reply_text(0, "Jakarta"))]
        run(store.retry(db, CH, "M15", NOW))
        del graph.fail["b1"]
        (after,) = _run_all(db, graph)
        assert after["status"] == store.JOB_DONE
        assert [c for c, _ in graph.posted] == ["a1", "b1"]

    def test_a_reply_that_went_out_despite_an_error_is_found_not_repeated(self):
        db = make_db([answer("a1", "Jakarta", "ana")])
        graph = Graph([MEET])

        class Lost(requests.Timeout):
            pass

        graph.fail["a1"] = Lost("timed out")
        _run_all(db, graph)
        # It did post: the thread shows our reply.
        graph.threads["a1"] = [{"comment_id": "r9", "author": "themapworld", "text": "Jakarta is live"}]
        del graph.fail["a1"]
        run(store.retry(db, CH, "M15", NOW))
        _run_all(db, graph)
        assert graph.posted == []
        assert db.city_requester_replies.docs[0]["status"] == store.TOLD

    def test_deleted_comments_are_skipped_without_stopping(self):
        db = make_db([answer("a1", "Jakarta", "ana", hours=3), answer("b1", "Jakarta", "bo", hours=2)])
        graph = Graph([MEET])
        graph.gone.add("a1")
        (after,) = _run_all(db, graph)
        assert after["status"] == store.JOB_DONE and [c for c, _ in graph.posted] == ["b1"]


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app import main
    from app.database import get_db
    from app.dependencies import verify_api_key

    db = make_db([answer("a1", "Jakarta", "ana")])
    main.app.dependency_overrides[get_db] = lambda: db
    main.app.dependency_overrides[verify_api_key] = lambda: None
    yield TestClient(main.app), db
    main.app.dependency_overrides.pop(get_db, None)
    main.app.dependency_overrides.pop(verify_api_key, None)


def test_routes(client):
    http, db = client
    base = f"/api/v1/channels/{CH}/city-requesters"
    preview = http.get(f"{base}/preview", params={"city": "jkt"}).json()
    assert preview["city"] == "Jakarta" and [p["username"] for p in preview["would_reply"]] == ["ana"]
    assert http.get(base).json()["setting"]["enabled"] is True
    assert http.put(f"{base}/settings", json={"enabled": False}).json()["enabled"] is False
    assert http.post(f"{base}/jobs/nope/retry").status_code == 404
    assert http.get(f"{base}/replies", params={"city": "Jakarta"}).json() == []
