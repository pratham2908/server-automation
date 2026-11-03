"""Commenter stats: the comment log (live rows, backfill sync) and the numbers worked out from it."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient

from app.services import comment_log
from app.services.comment_dm import CommentEvent
from app.services.commenter_stats import account_detail, commenter_stats
from app.services.instagram import next_endpoint
from app.timezone import IST, UTC
from tests.mongo_fakes import DB

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=IST)
CHANNEL = {
    "channel_id": "zk",
    "platform": "instagram",
    "instagram_user_id": "1784",
    "instagram_username": "zara.kapoor",
}


def _event(**kw: Any) -> CommentEvent:
    base = {"comment_id": "c1", "media_id": "m1", "text": "hi", "username": "Ravi", "from_id": "u1", "created_at": NOW}
    return CommentEvent(**{**base, **kw})


def _msg(cid: str, author: str, at: str = "2026-11-01T10:00:00+0000") -> dict[str, Any]:
    return {"comment_id": cid, "author": author, "text": f"text {cid}", "published_at": at}


# --- the log ---------------------------------------------------------------------------------


class TestLogRow:
    def row(self, **kw: Any) -> dict[str, Any] | None:
        base = {"comment_id": "c1", "media_id": "m1", "username": "Ravi", "text": "hi", "commented_at": NOW}
        return comment_log.log_row(CHANNEL, **{**base, **kw}, source="webhook", now=NOW)

    def test_author_is_the_lowercased_handle(self):
        row = self.row(username="@Ravi")
        assert row is not None and (row["author"], row["username"]) == ("ravi", "Ravi")

    def test_a_reply_is_flagged_by_its_parent(self):
        assert self.row(parent_id="c0")["is_reply"] is True
        assert self.row()["is_reply"] is False

    def test_own_comments_are_kept_but_flagged(self):
        assert self.row(from_id="1784", username="")["own"] is True
        assert self.row(username="Zara.Kapoor")["own"] is True
        assert self.row()["own"] is False

    def test_nothing_to_count_without_an_author(self):
        assert self.row(username="") is None
        assert self.row(username="", from_id="u7")["author"] == "id:u7"


@pytest.mark.asyncio
class TestRecord:
    async def test_a_comment_is_logged_once_however_often_it_arrives(self):
        db = DB()
        assert await comment_log.record(db, CHANNEL, _event(), source="webhook", now=NOW) is True
        assert await comment_log.record(db, CHANNEL, _event(), source="poll", now=NOW) is False
        assert len(db.comment_log.docs) == 1 and db.comment_log.docs[0]["source"] == "webhook"

    async def test_record_safely_swallows_a_failure(self):
        class Broken(DB):
            def __getattr__(self, name: str) -> Any:
                raise RuntimeError("mongo down")

        await comment_log.record_safely(Broken(), CHANNEL, _event(), source="webhook", now=NOW)


class TestRowsFromThreads:
    def test_a_reply_listed_as_top_level_too_counts_once_as_a_reply(self):
        threads = [
            {"top": _msg("r1", "anjali"), "replies": []},
            {"top": _msg("t1", "ravi"), "replies": [_msg("r1", "anjali")]},
            {"top": {**_msg("r2", "neha"), "parent_id": "t9"}, "replies": []},
        ]
        rows = comment_log.rows_from_threads(CHANNEL, "m1", threads, NOW)
        assert [(r["comment_id"], r["is_reply"], r["parent_id"]) for r in rows] == [
            ("t1", False, None),
            ("r1", True, "t1"),
            ("r2", True, "t9"),
        ]

    def test_replies_point_at_their_thread_and_own_replies_are_flagged(self):
        threads = [{"top": _msg("t1", "ravi"), "replies": [_msg("r1", "zara.kapoor"), _msg("r2", "anjali")]}]
        rows = comment_log.rows_from_threads(CHANNEL, "m1", threads, NOW)
        assert [(r["comment_id"], r["is_reply"], r["parent_id"], r["own"]) for r in rows] == [
            ("t1", False, None, False),
            ("r1", True, "t1", True),
            ("r2", True, "t1", False),
        ]
        assert rows[0]["commented_at"] == datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


class FakeReader:
    def __init__(self, posts: list[dict[str, Any]], threads: dict[str, list[dict[str, Any]]]) -> None:
        self.posts = posts
        self.threads = threads
        self.read: list[str] = []
        self.broken: set[str] = set()

    def list_comment_counts(self, ig_user_id: str) -> list[dict[str, Any]]:
        return self.posts

    def get_media_threads(self, media_id: str, max_threads: int = 50) -> list[dict[str, Any]]:
        self.read.append(media_id)
        if media_id in self.broken:
            raise requests.HTTPError("500")
        return self.threads.get(media_id, [])


@pytest.mark.asyncio
class TestSync:
    async def test_backfills_then_rereads_only_posts_whose_count_moved(self):
        db = DB(channels=[dict(CHANNEL)])
        reader = FakeReader(
            posts=[{"id": "m1", "comments_count": 2}, {"id": "m2", "comments_count": 1}, {"id": "m3"}],
            threads={
                "m1": [{"top": _msg("a", "ravi"), "replies": [_msg("b", "anjali")]}],
                "m2": [{"top": _msg("c", "ravi")}],
            },
        )
        first = await comment_log.sync_channel(db, db.channels.docs[0], reader, "1784", NOW)
        assert first == {"posts": 3, "read": 2, "added": 3, "failed": 0}
        assert reader.read == ["m1", "m2"] and db.channels.docs[0][comment_log.SYNCED_FIELD] == NOW

        # A comment the webhook already logged isn't counted twice when the sync reads it back.
        await comment_log.record(db, CHANNEL, _event(comment_id="d", media_id="m2"), source="webhook", now=NOW)
        reader.posts[1]["comments_count"] = 3
        reader.threads["m2"].append({"top": _msg("d", "ravi"), "replies": [_msg("e", "neha")]})
        reader.read.clear()
        second = await comment_log.sync_channel(db, db.channels.docs[0], reader, "1784", NOW)
        assert reader.read == ["m2"] and second["added"] == 1
        assert sorted(d["comment_id"] for d in db.comment_log.docs) == ["a", "b", "c", "d", "e"]

    async def test_a_post_that_fails_is_retried_next_time(self):
        db = DB(channels=[dict(CHANNEL)])
        reader = FakeReader(posts=[{"id": "m1", "comments_count": 1}], threads={"m1": [{"top": _msg("a", "ravi")}]})
        reader.broken.add("m1")
        assert (await comment_log.sync_channel(db, db.channels.docs[0], reader, "1784", NOW))["failed"] == 1
        reader.broken.clear()
        assert (await comment_log.sync_channel(db, db.channels.docs[0], reader, "1784", NOW))["added"] == 1


def test_next_page_links_on_a_newer_graph_version_still_page():
    link = "https://graph.instagram.com/v25.0/1794/comments?fields=id&after=QVF"
    assert next_endpoint(link) == "1794/comments?fields=id&after=QVF"
    assert next_endpoint("https://graph.facebook.com/v23.0/me/media?after=x") == "me/media?after=x"
    assert next_endpoint(None) is None


# --- the stats -------------------------------------------------------------------------------


def _row(author: str, media: str, ago_days: float, reply: bool = False, text: str = "") -> dict[str, Any]:
    at = NOW - timedelta(days=ago_days)
    cid = f"{author}-{media}-{ago_days}-{reply}"
    return {
        "comment_id": cid,
        "author": author,
        "username": author,
        "media_id": media,
        "is_reply": reply,
        "text": text or cid,
        # Mongo hands datetimes back naive UTC.
        "commented_at": at.astimezone(UTC).replace(tzinfo=None),
    }


ROWS = [
    # ravi: an old regular — 4 comments in the last week across 2 reels, one a reply; also commented 40 days ago.
    _row("ravi", "m1", 1),
    _row("ravi", "m1", 1.5, reply=True),
    _row("ravi", "m2", 2),
    _row("ravi", "m2", 3, text="newest? no"),
    _row("ravi", "m0", 40),
    # anjali: new this week, 2 comments on one reel.
    _row("anjali", "m2", 0.5, text="latest"),
    _row("anjali", "m2", 4),
    # neha: one comment this week.
    _row("neha", "m1", 6),
    # old: only seen 40 days ago.
    _row("old", "m0", 40),
]


class TestCommenterStats:
    def test_totals_for_a_week(self):
        stats = commenter_stats(ROWS, NOW, 7)
        assert stats["totals"] == {
            "comments": 7,
            "top_level": 6,
            "replies": 1,
            "accounts": 3,
            "videos": 2,
            "new_accounts": 2,
            "repeat_accounts": 2,
            "multi_video_accounts": 1,
            "repeat_share": round(6 / 7, 4),
            "top_share": 1.0,
            "per_account": round(7 / 3, 2),
        }

    def test_top_accounts_rank_by_comments_with_their_breakdown(self):
        top = commenter_stats(ROWS, NOW, 7)["top"]
        assert [a["author"] for a in top] == ["ravi", "anjali", "neha"]
        ravi, anjali = top[0], top[1]
        assert (ravi["comments"], ravi["top_level"], ravi["replies"], ravi["videos"]) == (4, 3, 1, 2)
        assert ravi["is_new"] is False and anjali["is_new"] is True
        assert anjali["last_text"] == "latest"
        assert ravi["first_seen_at"].startswith((NOW - timedelta(days=40)).date().isoformat())

    def test_all_time_counts_everyone_as_new_and_spreads_accounts_by_count(self):
        stats = commenter_stats(ROWS, NOW, None)
        assert stats["totals"]["accounts"] == stats["totals"]["new_accounts"] == 4
        assert {d["label"]: d["accounts"] for d in stats["distribution"]} == {
            "1": 2,
            "2": 1,
            "3–5": 1,
            "6–10": 0,
            "11+": 0,
        }
        assert stats["tracked_since"].startswith((NOW - timedelta(days=40)).date().isoformat())

    def test_daily_series_covers_the_window_by_ist_day(self):
        series = commenter_stats(ROWS, NOW, 7)["series"]
        assert len(series) == 7 and series[-1]["date"] == "2026-11-03"
        by_day = {d["date"]: d for d in series}
        assert by_day["2026-11-02"] == {"date": "2026-11-02", "comments": 2, "accounts": 1, "new_accounts": 0}
        assert by_day["2026-10-30"]["new_accounts"] == 1  # anjali's first comment, 4 days back
        assert len(commenter_stats(ROWS, NOW, None)["series"]) == 30

    def test_no_comments_is_all_zeros(self):
        stats = commenter_stats([], NOW, 30)
        assert stats["totals"]["accounts"] == 0 and stats["top"] == [] and stats["tracked_since"] is None


def test_account_detail_lists_their_comments_newest_first_with_reel_titles():
    detail = account_detail([r for r in ROWS if r["author"] == "ravi"], {"m1": "Saree reel"})
    assert (detail["comments"], detail["top_level"], detail["replies"], detail["videos"]) == (5, 4, 1, 3)
    assert detail["items"][0]["video_title"] == "Saree reel" and detail["items"][-1]["media_id"] == "m0"
    assert account_detail([], {}) is None


# --- routes ----------------------------------------------------------------------------------


@pytest.fixture
def client():
    from app import main
    from app.database import get_db
    from app.dependencies import verify_api_key

    own = {**ROWS[0], "comment_id": "own1", "author": "zara.kapoor", "own": True}
    db = DB(
        channels=[dict(CHANNEL)],
        comment_log=[{**r, "channel_id": "zk"} for r in ROWS] + [{**own, "channel_id": "zk"}],
        videos=[{"channel_id": "zk", "instagram_media_id": "m2", "title": "Gym reel"}],
    )
    main.app.dependency_overrides[get_db] = lambda: db
    main.app.dependency_overrides[verify_api_key] = lambda: None
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(get_db, None)
    main.app.dependency_overrides.pop(verify_api_key, None)


def test_stats_route_leaves_out_the_channels_own_comments(client):
    body = client.get("/api/v1/channels/zk/commenters", params={"window": "all"}).json()
    assert body["window"] == "all" and body["totals"]["accounts"] == 4
    assert "zara.kapoor" not in [a["author"] for a in body["top"]]
    assert client.get("/api/v1/channels/nope/commenters").status_code == 404


def test_account_route(client):
    body = client.get("/api/v1/channels/zk/commenters/accounts/Anjali").json()
    assert body["comments"] == 2 and body["items"][0]["video_title"] == "Gym reel"
    assert client.get("/api/v1/channels/zk/commenters/accounts/nobody").status_code == 404


def test_threads_name_viewers_from_the_from_field():
    """Instagram leaves ``username`` empty for everyone but the account itself; ``from`` has the handle."""
    from app.services.instagram import InstagramService

    viewer = InstagramService._thread_message({"id": "c1", "username": None, "from": {"id": "42", "username": "ravi"}})
    ours = InstagramService._thread_message({"id": "c2", "username": "zara.kapoor", "from": {"id": "1784"}})
    assert (viewer["author"], viewer["author_id"]) == ("ravi", "42")
    assert (ours["author"], ours["author_id"]) == ("zara.kapoor", "1784")


def test_nameless_replies_are_named_by_reading_them_by_id_50_at_a_time():
    """Nested replies come back as id and text only; a multi-id read names them."""
    from app.services.instagram import InstagramService

    calls: list[dict[str, str]] = []

    def fake_get(endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        calls.append(params)
        return {i: {"id": i, "from": {"id": f"u{i}", "username": f"fan{i}"}} for i in params["ids"].split(",")}

    service = InstagramService.__new__(InstagramService)
    service._get = fake_get  # type: ignore[method-assign]
    replies = [{"comment_id": str(i), "author": "", "author_id": ""} for i in range(60)]
    named = {"comment_id": "x", "author": "zara.kapoor", "author_id": "1784"}
    service._fill_reply_authors([{"top": {}, "replies": [*replies[:30], named]}, {"top": {}, "replies": replies[30:]}])
    assert [len(c["ids"].split(",")) for c in calls] == [50, 10]
    assert replies[59]["author"] == "fan59" and replies[0]["author_id"] == "u0"
    assert named["author"] == "zara.kapoor"


@pytest.mark.asyncio
async def test_stored_webhook_comments_are_imported_once_and_the_sync_row_wins():
    naive = datetime(2026, 10, 30, 10, 0)
    db = DB(
        channels=[dict(CHANNEL)],
        comment_dm_events=[
            {
                "comment_id": "d1",
                "channel_id": "zk",
                "media_id": "m9",
                "username": "neha",
                "from_id": "u3",
                "parent_id": "t0",
                "commented_at": naive,
            }
        ],
        comment_ai_reply_queue=[
            {
                "comment_id": "q1",
                "channel_id": "zk",
                "media_id": "m9",
                "username": "ravi",
                "skip_reason": "a reply inside a thread",
                "commented_at": naive,
            },
            {
                "comment_id": "q2",
                "channel_id": "zk",
                "media_id": "m9",
                "username": "zk_alt",
                "skip_reason": "our own comment",
                "commented_at": naive,
            },
            {
                "comment_id": "q3",
                "channel_id": "zk",
                "media_id": "m9",
                "username": "anjali",
                "skip_reason": None,
                "commented_at": naive,
            },
            {
                "comment_id": "s1",
                "channel_id": "zk",
                "media_id": "m1",
                "username": "ravi",
                "skip_reason": "a reply inside a thread",
                "commented_at": naive,
            },
            {"comment_id": "x1", "channel_id": "gone", "media_id": "m1", "username": "ravi", "commented_at": naive},
        ],
    )
    synced = comment_log.rows_from_threads(CHANNEL, "m1", [{"top": _msg("s1", "ravi")}], NOW)
    await db.comment_log.insert_one(synced[0])

    assert await comment_log.import_stored_comments(db, NOW) == 4
    rows = {d["comment_id"]: d for d in db.comment_log.docs}
    assert (rows["d1"]["is_reply"], rows["d1"]["parent_id"]) == (True, "t0")
    assert rows["q1"]["is_reply"] is True and rows["q3"]["is_reply"] is False
    assert rows["q2"]["own"] is True
    assert rows["s1"]["source"] == "sync" and rows["s1"]["is_reply"] is False
    assert "x1" not in rows
    assert await comment_log.import_stored_comments(db, NOW) == 0
