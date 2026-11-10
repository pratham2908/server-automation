"""Voted-city reels: the live vote, the kit, spotting the live reel, and the follow-up posts."""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient

from app.services import comment_collectors as cc
from app.services import voted_city as vc
from app.services import voted_city_store as store
from tests.mongo_fakes import DB

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
CH = "DreamScenicAi"


def run(coro):
    return asyncio.run(coro)


def _graph_error(code: int, status_code: int = 400) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    response._content = json.dumps({"error": {"code": code}}).encode()
    return requests.HTTPError(str(status_code), response=response)


class Graph:
    """Instagram, as far as the vote and the follow-up use it."""

    def __init__(self, likes: dict[str, int] | None = None, media: list[dict[str, Any]] | None = None) -> None:
        self.likes = likes or {}
        self.media = media or []
        self.posted: list[tuple[str, str, str]] = []  # (kind, target, text)
        self.fail: list[Exception] = []  # raised by the next posts, in order
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.replies: dict[str, list[dict[str, Any]]] = {}
        self._next = 900

    def get_comment_like_counts(self, ids: list[str]) -> dict[str, int]:
        return {i: self.likes[i] for i in ids if i in self.likes}

    def get_permalinks(self, ids: list[str]) -> dict[str, str]:
        return {i: f"https://www.instagram.com/reel/SC{i}/" for i in ids}

    def get_media_page(self, ig_user_id: str, *, limit: int = 25, after: str | None = None) -> dict[str, Any]:
        return {"data": self.media, "next_cursor": None}

    def _post(self, kind: str, target: str, text: str) -> str:
        if self.fail:
            exc = self.fail.pop(0)
            if getattr(exc, "posted_anyway", False):
                self._record(kind, target, text)
            raise exc
        return self._record(kind, target, text)

    def _record(self, kind: str, target: str, text: str) -> str:
        self._next += 1
        cid = str(self._next)
        self.posted.append((kind, target, text))
        mine = {"comment_id": cid, "author": "themapworld", "text": text}
        (self.comments if kind == "comment" else self.replies).setdefault(target, []).append(mine)
        return cid

    def post_comment(self, media_id: str, message: str) -> str:
        return self._post("comment", media_id, message)

    def reply_to_comment(self, comment_id: str, message: str) -> str:
        return self._post("reply", comment_id, message)

    def get_media_comments(self, media_id: str) -> list[dict[str, Any]]:
        return self.comments.get(media_id, [])

    def get_thread(self, comment_id: str, media_id: str = "") -> dict[str, Any]:
        return {"replies": self.replies.get(comment_id, [])}


class R2:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def upload_video(self, stream, key: str) -> None:
        self.files[key] = stream.read()

    def generate_presigned_url(self, key: str, expires_in: int = 3600, response_content_type: str | None = None) -> str:
        return f"https://r2.example/{key}?ttl={expires_in}"


def answer(cid: str, city: str, who: str, likes: int | None, media: str = "m1", **over: Any) -> dict[str, Any]:
    row = {
        "collector_id": "col",
        "channel_id": CH,
        "comment_id": cid,
        "media_id": media,
        "author": who,
        "username": who,
        "text": f"do {city} please",
        "answer": city,
        "answer_key": cc.fold(city),
        "commented_at": NOW - timedelta(hours=int(cid)),
        "created_at": NOW,
    }
    if likes is not None:
        row["like_count"] = likes
    row.update(over)
    return row


def collector(**over: Any) -> dict[str, Any]:
    doc = {
        "id": "col",
        "channel_id": CH,
        "name": "City requests",
        "question": "Which city do they ask for?",
        "enabled": True,
        "merges": {"hk": "Hong Kong", "香港": "Hong Kong"},
        "active_from": NOW - timedelta(days=30),
        "created_at": NOW - timedelta(days=30),
        "updated_at": NOW - timedelta(days=30),
    }
    doc.update(over)
    return doc


def make_db(rows: list[dict[str, Any]], **more: Any) -> DB:
    return DB(
        channels=[{"channel_id": CH, "instagram_username": "themapworld", "instagram_user_id": "ig1"}],
        comment_collectors=[collector()],
        collector_answers=rows,
        **more,
    )


@pytest.fixture(autouse=True)
def quiet_mail(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_send(settings, recipient, subject, body, html_body=None):
        sent.append((subject, body))
        return True

    async def owner(db, settings):
        return "owner@example.com"

    class Errors:
        def __init__(self) -> None:
            self.logged: list[str] = []

        async def log_error(self, feature, message, exception=None, context=None):
            self.logged.append(message)

    errors = Errors()
    monkeypatch.setattr(store, "send_email", fake_send)
    monkeypatch.setattr(store, "resolve_owner_email", owner)
    monkeypatch.setattr(store, "get_error_service", lambda db: errors)
    return sent, errors


class Settings:
    ANALYZER_PUBLIC_URL = "https://app.example"


# ---- Rules ----------------------------------------------------------------------------------------------


class TestRules:
    def test_vote_sums_likes_per_city_through_aliases(self):
        rows = [
            answer("1", "Hong Kong", "ana", 300),
            answer("2", "HK", "bo", 50),
            answer("3", "香港", "cy", 24),
            answer("4", "Poland", "dee", 88),
            answer("5", "Hong Kong", "ana", 2, media="m2"),
        ]
        groups = vc.city_groups(rows, {"hk": "Hong Kong", "香港": "Hong Kong"})
        assert [(g["city"], g["likes"], g["comments"], g["people"]) for g in groups] == [
            ("Hong Kong", 376, 4, 3),
            ("Poland", 88, 1, 1),
        ]
        assert groups[0]["top"]["comment_id"] == "1"
        assert [c["comment_id"] for c in groups[0]["others"]] == ["2", "3", "5"]

    def test_deleted_own_and_done_cities_do_not_count(self):
        rows = [
            answer("1", "Hong Kong", "ana", 300),
            answer("2", "Vienna", "bo", 45, comment_gone=True),
            answer("3", "Vienna", "themapworld", 99),
            answer("4", "Vienna", "cy", 5),
        ]
        groups = vc.city_groups(rows, {}, done=["hong kong"], own_usernames=["themapworld"])
        assert [(g["city"], g["likes"]) for g in groups] == [("Vienna", 5)]

    def test_a_tie_goes_to_whoever_asked_first(self):
        groups = vc.city_groups([answer("1", "Rome", "a", 4), answer("9", "Rome", "b", 4)], {})
        assert groups[0]["top"]["comment_id"] == "9"  # written 9 hours ago, before comment 1

    def test_caption_first_line_is_the_servers(self):
        assert vc.compose_caption("Hong Kong", 374, "Two friends, 3 roads.\n#maps") == (
            "You voted. Hong Kong won 📍 374 likes\n\nTwo friends, 3 roads.\n#maps"
        )
        assert vc.compose_caption("Rome", 1, None) == "You voted. Rome won 📍 1 like"
        with pytest.raises(ValueError):
            vc.compose_caption("Rome", 5, "You voted. Rome won 📍 9000 likes")

    def test_replies_rotate(self):
        assert [vc.reply_text(i, "Rome") for i in range(4)] == [
            "Rome is up. You picked it 🙌",
            "Done! Rome is live now 📍",
            "Rome is up. You picked it 🔥",
            "Rome is up. You picked it 🙌",
        ]

    def test_matching_a_post_to_a_kit(self):
        kit = {"city": "Hong Kong", "caption": "You voted. Hong Kong won 📍 374 likes\n\nbody", "created_at": NOW}
        later = "2026-11-09T12:30:00+0000"
        assert vc.matches_kit(kit, {"caption": "You voted. Hong Kong won 📍 374 likes\n\nedited", "timestamp": later})
        assert vc.matches_kit(kit, {"caption": "You voted.  hong kong won!", "timestamp": later})
        assert not vc.matches_kit(kit, {"caption": "You voted. Rome won 📍 9 likes", "timestamp": later})
        assert not vc.matches_kit(kit, {"caption": "Hong Kong at night", "timestamp": later})
        assert not vc.matches_kit(kit, {"caption": kit["caption"], "timestamp": "2026-11-08T12:00:00+0000"})

    def test_deep_link(self):
        code = vc.shortcode("https://www.instagram.com/reel/DQx1_abc/")
        assert vc.deep_link(code, "181") == "https://www.instagram.com/reel/DQx1_abc/c/181/"
        assert vc.deep_link(vc.shortcode(None), "181") is None

    def test_refusal_versus_unsure(self):
        assert vc.refused(_graph_error(10))
        assert vc.target_gone(_graph_error(100))
        assert not vc.refused(_graph_error(2, status_code=500))
        assert not vc.refused(requests.Timeout("slow"))


# ---- The live vote --------------------------------------------------------------------------------------


class TestTally:
    def test_reads_likes_live_and_keeps_them(self):
        db = make_db([answer("1", "Hong Kong", "ana", 3), answer("2", "Poland", "bo", 88), answer("3", "Rome", "c", 9)])
        graph = Graph(likes={"1": 369, "2": 90})  # comment 3 was deleted
        vote = run(store.tally(db, graph, CH, NOW))
        assert [(c["city"], c["likes"]) for c in vote["cities"]] == [("Hong Kong", 369), ("Poland", 90)]
        assert vote["cities"][0]["top"]["deep_link"] == "https://www.instagram.com/reel/SCm1/c/1/"
        kept = {r["comment_id"]: r for r in db.collector_answers.docs}
        assert kept["1"]["like_count"] == 369 and kept["3"]["comment_gone"] is True
        assert db.media_permalinks.docs[0]["media_id"] == "m1"

    def test_done_cities_are_left_out(self):
        db = make_db([answer("1", "Hong Kong", "ana", 3), answer("2", "Poland", "bo", 1)])
        run(store.mark_done(db, CH, "Hong Kong", NOW, media_id="18118107794042638"))
        run(store.mark_done(db, CH, "hong kong", NOW))  # again: the first record stays
        vote = run(store.tally(db, Graph(likes={"1": 5, "2": 1}), CH, NOW))
        assert [c["city"] for c in vote["cities"]] == ["Poland"]
        assert len(vote["done"]) == 1 and vote["done"][0]["media_id"] == "18118107794042638"

    def test_live_needs_instagram(self):
        db = make_db([answer("1", "Rome", "a", 1)])
        with pytest.raises(store.VoteError):
            run(store.tally(db, None, CH, NOW))
        assert run(store.tally(db, None, CH, NOW, live=False))["cities"][0]["likes"] == 1


# ---- Kits -----------------------------------------------------------------------------------------------


def _kit(db: DB, graph: Graph, r2: R2 | None = None, votes: int = 374, city: str = "hk", body: str | None = "Body"):
    return run(
        store.create_kit(
            db,
            graph,
            r2 or R2(),
            Settings(),  # type: ignore[arg-type]
            CH,
            city=city,
            votes=votes,
            video=io.BytesIO(b"mp4"),
            caption_body=body,
            now=NOW,
        )
    )


def _hk_db() -> tuple[DB, Graph]:
    rows = [
        answer("1", "Hong Kong", "ana", 0),
        answer("2", "Hong Kong", "bo", 0),
        answer("3", "Hong Kong", "cy", 0),
        answer("4", "Hong Kong", "ana", 0),  # ana again: thanked once at most
        answer("5", "Hong Kong", "dee", 0),
        answer("6", "Hong Kong", "eve", 0),
        answer("7", "Poland", "fay", 0),
    ]
    return make_db(rows), Graph(likes={"1": 300, "2": 40, "3": 20, "4": 10, "5": 3, "6": 1, "7": 88})


class TestKits:
    def test_kit_is_made_from_the_live_vote(self, quiet_mail):
        sent, _ = quiet_mail
        db, graph = _hk_db()
        r2 = R2()
        kit = _kit(db, graph, r2)
        assert kit["city"] == "Hong Kong" and kit["status"] == "ready"
        assert kit["caption"] == "You voted. Hong Kong won 📍 374 likes\n\nBody"
        assert kit["live"]["likes"] == 374 and kit["top"]["comment_id"] == "1"
        assert [c["comment_id"] for c in kit["candidates"]] == ["2", "3", "4", "5", "6"]
        assert r2.files[kit["r2_object_key"]] == b"mp4"
        assert sent and "Hong Kong" in sent[0][0] and "https://www.instagram.com/reel/SCm1/c/1/" in sent[0][1]
        out = store.kit_out(kit, r2)
        assert out["download_url"].startswith("https://r2.example/voted-kits/")

    def test_claiming_more_likes_than_live_is_refused(self):
        db, graph = _hk_db()
        with pytest.raises(store.VoteError, match="374 live likes"):
            _kit(db, graph, votes=375)
        assert not db.voted_reel_kits.docs

    def test_unknown_done_and_duplicate_cities_are_refused(self):
        db, graph = _hk_db()
        with pytest.raises(store.VoteError, match="nobody has asked"):
            _kit(db, graph, city="Lima", votes=1)
        _kit(db, graph)
        with pytest.raises(store.VoteError, match="open kit"):
            _kit(db, graph)
        run(store.mark_done(db, CH, "Poland", NOW))
        with pytest.raises(store.VoteError, match="already posted"):
            _kit(db, graph, city="Poland", votes=1)

    def test_a_cancelled_kit_frees_the_city(self):
        db, graph = _hk_db()
        kit = _kit(db, graph)
        assert run(store.cancel_kit(db, kit, NOW))["status"] == "cancelled"
        assert _kit(db, graph)["status"] == "ready"


# ---- Spotting the live reel and following up -----------------------------------------------------------

LIVE = {
    "id": "M9",
    "caption": "You voted. Hong Kong won 📍 374 likes\n\nBody",
    "timestamp": "2026-11-09T13:00:00+0000",
    "permalink": "https://www.instagram.com/reel/HKX/",
}


def _live_kit() -> tuple[DB, Graph, dict[str, Any]]:
    db, graph = _hk_db()
    kit = _kit(db, graph)
    graph.media = [LIVE]
    report = run(store.detect(db, graph, CH, "ig1", NOW))
    assert report == [
        {"media_id": "M9", "caption": vc.caption_head(LIVE["caption"]), "kit_id": kit["kit_id"], "outcome": "linked"}
    ]
    return db, graph, run(store.get_kit(db, CH, kit["kit_id"]))


def _advance(db: DB, graph: Graph, kit: dict[str, Any], at: datetime = NOW) -> dict[str, Any]:
    return run(store.advance(db, graph, run(store.get_kit(db, CH, kit["kit_id"])), "themapworld", at))


class TestFollowUp:
    def test_detect_links_the_reel_and_marks_the_city_done(self):
        db, graph, kit = _live_kit()
        assert kit["status"] == "live" and kit["media_id"] == "M9"
        assert db.voted_cities.docs[0]["city_key"] == "hong kong"
        # Seen again: nothing changes.
        assert run(store.detect(db, graph, CH, "ig1", NOW))[0]["outcome"] == "already linked"

    def test_hand_posted_hong_kong_is_left_alone(self):
        """Regression: 11-09's Hong Kong reel was posted and followed up by hand."""
        db, graph = _hk_db()
        run(
            store.mark_done(
                db,
                CH,
                "Hong Kong",
                NOW,
                media_id="18118107794042638",
                comment_ids=["18170461615469943", "18098828282129914", "18132806782830117", "18030562757877774"],
            )
        )
        graph.media = [{**LIVE, "id": "18118107794042638"}]
        dry = run(store.detect(db, graph, CH, "ig1", NOW, dry_run=True))
        assert dry[0]["outcome"] == "city already marked done"
        run(store.detect(db, graph, CH, "ig1", NOW))
        assert graph.posted == [] and not db.voted_reel_kits.docs

    def test_dry_run_links_nothing(self):
        db, graph = _hk_db()
        _kit(db, graph)
        graph.media = [LIVE]
        assert run(store.detect(db, graph, CH, "ig1", NOW, dry_run=True))[0]["outcome"] == "would link"
        assert db.voted_reel_kits.docs[0]["status"] == "ready"

    def test_first_comment_then_three_different_people_one_per_pass(self):
        db, graph, kit = _live_kit()
        for _ in range(6):
            kit = _advance(db, graph, kit)
        assert graph.posted == [
            ("comment", "M9", vc.FIRST_COMMENT),
            ("reply", "2", "Hong Kong is up. You picked it 🙌"),
            ("reply", "3", "Done! Hong Kong is live now 📍"),
            ("reply", "5", "Hong Kong is up. You picked it 🔥"),  # 4 is ana again, the top commenter
        ]
        assert kit["status"] == "done"
        assert [kit["steps"][n]["comment_id"] for n in kit["step_order"]] == ["901", "902", "903", "904"]

    def test_a_post_that_may_have_gone_through_is_checked_not_resent(self):
        db, graph, kit = _live_kit()
        timeout = requests.Timeout("read timed out")
        timeout.posted_anyway = True  # type: ignore[attr-defined]
        graph.fail = [timeout]
        kit = _advance(db, graph, kit)
        assert kit["steps"]["first_comment"]["state"] == "unsure"
        kit = _advance(db, graph, kit)
        assert kit["steps"]["first_comment"] == {**kit["steps"]["first_comment"], "state": "done", "comment_id": "901"}
        assert graph.posted.count(("comment", "M9", vc.FIRST_COMMENT)) == 1

    def test_a_post_that_did_not_go_through_is_sent_again(self):
        db, graph, kit = _live_kit()
        graph.fail = [requests.ConnectionError("reset")]
        _advance(db, graph, kit)
        kit = _advance(db, graph, kit)
        assert kit["steps"]["first_comment"]["state"] == "done"
        assert graph.posted == [("comment", "M9", vc.FIRST_COMMENT)]

    def test_a_restart_mid_call_is_checked_too(self):
        db, graph, kit = _live_kit()
        kit = run(store.plan_steps(db, graph, kit, NOW))
        run(store._set_step(db, kit["kit_id"], "first_comment", "pending", {"state": "claimed", "claimed_at": NOW}))
        _advance(db, graph, kit, at=NOW + timedelta(minutes=5))
        assert graph.posted == []  # still in flight, maybe
        kit = _advance(db, graph, kit, at=NOW + timedelta(minutes=11))
        assert graph.posted == [("comment", "M9", vc.FIRST_COMMENT)]

    def test_a_deleted_comment_is_skipped(self):
        db, graph, kit = _live_kit()
        kit = _advance(db, graph, kit)
        graph.fail = [_graph_error(100)]
        kit = _advance(db, graph, kit)
        assert kit["steps"]["reply_1"]["state"] == "skipped"

    def test_refusals_fail_after_three_tries_and_tell_the_owner_once(self, quiet_mail):
        sent, errors = quiet_mail
        db, graph, kit = _live_kit()
        kit = _advance(db, graph, kit)  # first comment
        graph.fail = [_graph_error(10)] * 3
        for _ in range(3):
            kit = _advance(db, graph, kit)
            run(store.report_attention(db, Settings(), kit, NOW))  # type: ignore[arg-type]
        assert kit["steps"]["reply_1"]["state"] == "failed"
        for _ in range(3):
            kit = _advance(db, graph, kit)
            run(store.report_attention(db, Settings(), kit, NOW))  # type: ignore[arg-type]
        assert kit["status"] == "attention"
        assert len(errors.logged) == 1 and sum("follow-up failed" in s for s, _ in sent) == 1
        assert any(not e["ok"] for e in kit["log"])
        # Retry: checked for an earlier post, then sent.
        kit = run(store.retry_failed(db, kit, NOW))
        kit = _advance(db, graph, kit)
        assert kit["steps"]["reply_1"]["state"] == "done" and kit["status"] == "done"


# ---- Routes ---------------------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    from app import main
    from app.database import get_db
    from app.dependencies import verify_api_key
    from app.routers import voted_city as routes

    db, graph = _hk_db()
    r2 = R2()

    async def conn(db_, manager, channel_id):
        return graph, "ig1", "themapworld"

    monkeypatch.setattr(routes, "graph_for", conn)
    monkeypatch.setattr(routes, "_deps", lambda: (object(), r2))
    monkeypatch.setattr(routes, "get_settings", Settings)
    main.app.dependency_overrides[get_db] = lambda: db
    main.app.dependency_overrides[verify_api_key] = lambda: None
    yield TestClient(main.app), db, graph
    main.app.dependency_overrides.pop(get_db, None)
    main.app.dependency_overrides.pop(verify_api_key, None)


def test_routes_end_to_end(client):
    http, db, graph = client
    base = f"/api/v1/channels/{CH}/city-vote"
    vote = http.get(base).json()
    assert vote["cities"][0]["city"] == "Hong Kong" and vote["cities"][0]["likes"] == 374
    bad = http.post(
        f"{base}/kits", data={"city": "Hong Kong", "votes": "999"}, files={"file": ("r.mp4", b"x", "video/mp4")}
    )
    assert bad.status_code == 409 and "live likes" in bad.json()["detail"]
    made = http.post(
        f"{base}/kits",
        data={"city": "Hong Kong", "votes": "374", "caption_body": "Two friends"},
        files={"file": ("r.mp4", b"mp4", "video/mp4")},
    )
    assert made.status_code == 201, made.text
    kit = made.json()
    assert kit["download_url"] and kit["top"]["deep_link"].endswith("/c/1/")
    # Posted just now: the route makes the kit at the real current time, and a post older than its kit is no match.
    posted = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")
    graph.media = [{**LIVE, "caption": kit["caption"], "timestamp": posted}]
    checked = http.post(f"{base}/kits/{kit['kit_id']}/check").json()
    assert checked["status"] == "live" and checked["steps"][0]["state"] == "done"
    assert [k["kit_id"] for k in http.get(f"{base}/kits").json()] == [kit["kit_id"]]
    assert http.get(f"{base}/done").json()[0]["city"] == "Hong Kong"
    assert http.get(f"{base}/recent").json()[0]["outcome"] == "already linked"
    assert http.post(f"{base}/kits/{kit['kit_id']}/cancel").status_code == 409
    assert http.get(f"{base}/kits/nope").status_code == 404
    assert http.post(f"{base}/done", json={"city": "Rome"}).status_code == 201
    assert http.delete(f"{base}/done/rome").json() == {"removed": True}
