"""Reading threads, drafting with the whole thread, and sending: the inbox's behaviour end to end, on fakes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.routers.comment_inbox as inbox_router
from app.main import app
from app.services.comment_inbox import (
    ThreadNotFoundError,
    draft_reply,
    list_threads,
    load_thread,
    send_reply,
)
from tests.conftest import GLOBAL_API_KEY

CHANNEL = {"channel_id": "ch", "platform": "instagram", "instagram_username": "tryalgoviz"}
VIDEO = {
    "video_id": "v1",
    "title": "A* vs Dijkstra",
    "description": "Pathfinding explained",
    "instagram_media_id": "m1",
}


def _raw_thread() -> dict[str, Any]:
    return {
        "top": {
            "comment_id": "t1",
            "text": "how does it handle cycles?",
            "author": "fan",
            "published_at": "2026-10-10T01:00:00",
            "like_count": 3,
        },
        "replies": [
            {
                "comment_id": "r1",
                "text": "it tracks visited nodes",
                "author": "tryalgoviz",
                "published_at": "2026-10-10T02:00:00",
            },
            {"comment_id": "r2", "text": "and weighted edges?", "author": "fan", "published_at": "2026-10-10T03:00:00"},
        ],
        "comment_url": "https://example/c/t1",
    }


class _Reader:
    def __init__(self, raw: list[dict[str, Any]] | None = None, missing: bool = False) -> None:
        self.raw = raw if raw is not None else [_raw_thread()]
        self.missing = missing
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def get_media_threads(self, media_id: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append(("media", (media_id, limit)))
        return self.raw

    def get_video_threads(self, video_id: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append(("video", (video_id, limit)))
        return self.raw

    def get_thread(self, thread_id: str, media_id: str = "") -> dict[str, Any]:
        self.calls.append(("thread", (thread_id, media_id)))
        if self.missing:
            raise ValueError("gone")
        return _raw_thread()


class _Poster:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def reply_to_comment(self, comment_id: str, text: str) -> str:
        self.sent.append((comment_id, text))
        return "new-reply"


class _Gemini:
    def __init__(self, text: str = "yep, handles it fine") -> None:
        self.text = text
        self.kwargs: dict[str, Any] = {}

    async def generate_thread_reply(self, **kwargs: Any) -> str:
        self.kwargs = kwargs
        return self.text


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        return self._rows


class _Replies:
    def __init__(self, pending: list[dict[str, Any]] | None = None) -> None:
        self.pending = pending or []
        self.upserts: list[tuple[dict[str, Any], dict[str, Any], bool]] = []

    def find(self, query: dict[str, Any]) -> _Cursor:
        ids = query["comment_id"]["$in"]
        return _Cursor([r for r in self.pending if r["comment_id"] in ids])

    async def update_one(self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False) -> None:
        self.upserts.append((query, update, upsert))


class _Db:
    def __init__(self, replies: _Replies | None = None) -> None:
        self.comment_replies = replies or _Replies()


# --- reading ------------------------------------------------------------------


def test_threads_are_read_from_the_platforms_media_id_and_shaped_with_who_spoke_last():
    reader = _Reader()
    threads = asyncio.run(list_threads(_Db(), CHANNEL, reader, VIDEO, 25))  # type: ignore[arg-type]
    assert reader.calls == [("media", ("m1", 25))]
    assert threads[0]["thread_id"] == "t1" and threads[0]["reply_count"] == 2
    assert threads[0]["replies"][0]["is_own"] and not threads[0]["replies"][1]["is_own"]
    assert threads[0]["needs_reply"]  # the viewer asked a follow-up after our answer


def test_a_youtube_channel_reads_through_the_video_threads_method():
    reader = _Reader()
    channel = {"channel_id": "yt", "platform": "youtube", "youtube_channel_id": "UC-me"}
    video = {"video_id": "v", "youtube_video_id": "yt123"}
    asyncio.run(list_threads(_Db(), channel, reader, video, 10))  # type: ignore[arg-type]
    assert reader.calls == [("video", ("yt123", 10))]


def test_a_video_that_was_never_published_has_no_threads_and_no_platform_call():
    reader = _Reader()
    assert asyncio.run(list_threads(_Db(), CHANNEL, reader, {"video_id": "v"}, 10)) == []  # type: ignore[arg-type]
    assert reader.calls == []


def test_a_draft_waiting_in_the_review_queue_is_attached_to_its_thread():
    db = _Db(_Replies([{"comment_id": "t1", "reply_text": "drafted earlier", "status": "pending_approval"}]))
    threads = asyncio.run(list_threads(db, CHANNEL, _Reader(), VIDEO, 10))  # type: ignore[arg-type]
    assert threads[0]["pending_draft"] == "drafted earlier"


def test_a_thread_that_has_gone_missing_raises_not_found():
    with pytest.raises(ThreadNotFoundError):
        asyncio.run(load_thread(CHANNEL, _Reader(missing=True), VIDEO, "t1"))


# --- drafting -----------------------------------------------------------------


def test_the_model_is_given_the_whole_thread_the_video_and_the_instruction():
    gemini = _Gemini()
    thread = asyncio.run(load_thread(CHANNEL, _Reader(), VIDEO, "t1"))
    asyncio.run(draft_reply(gemini, thread, VIDEO, instruction="keep it short"))
    transcript = gemini.kwargs["transcript"]
    assert "how does it handle cycles?" in transcript  # the opening comment
    assert "You (the channel): it tracks visited nodes" in transcript  # our earlier answer
    assert "and weighted edges?   <-- reply to this" in transcript  # the follow-up, marked
    assert (
        "A* vs Dijkstra" in gemini.kwargs["video_context"] and "Pathfinding explained" in gemini.kwargs["video_context"]
    )
    assert gemini.kwargs["instruction"] == "keep it short" and gemini.kwargs["platform"] == "instagram"


def test_with_no_target_the_draft_answers_the_newest_viewer_message_and_mentions_them():
    thread = asyncio.run(load_thread(CHANNEL, _Reader(), VIDEO, "t1"))
    draft = asyncio.run(draft_reply(_Gemini("it does"), thread, VIDEO))
    assert draft == {"text": "@fan it does", "target_comment_id": "r2"}


def test_drafting_for_the_opening_comment_adds_no_mention():
    thread = asyncio.run(load_thread(CHANNEL, _Reader(), VIDEO, "t1"))
    draft = asyncio.run(draft_reply(_Gemini("it does"), thread, VIDEO, target_comment_id="t1"))
    assert draft["text"] == "it does"


def test_drafting_for_a_message_that_is_not_in_the_thread_is_refused():
    thread = asyncio.run(load_thread(CHANNEL, _Reader(), VIDEO, "t1"))
    with pytest.raises(ThreadNotFoundError):
        asyncio.run(draft_reply(_Gemini(), thread, VIDEO, target_comment_id="nope"))


def test_an_empty_model_answer_stays_empty_instead_of_becoming_a_bare_mention():
    thread = asyncio.run(load_thread(CHANNEL, _Reader(), VIDEO, "t1"))
    assert asyncio.run(draft_reply(_Gemini(""), thread, VIDEO))["text"] == ""


# --- sending ------------------------------------------------------------------


def test_a_reply_to_a_nested_message_is_posted_under_the_opening_comment_with_a_mention():
    poster, db = _Poster(), _Db()
    thread = asyncio.run(load_thread(CHANNEL, _Reader(), VIDEO, "t1"))
    sent = asyncio.run(send_reply(db, CHANNEL, poster, thread, VIDEO, "it does", "r2"))  # type: ignore[arg-type]
    assert poster.sent == [("t1", "@fan it does")]
    assert sent["reply_id"] == "new-reply"


def test_sending_records_the_reply_and_supersedes_any_draft_for_that_thread():
    poster, replies = _Poster(), _Replies()
    thread = asyncio.run(load_thread(CHANNEL, _Reader(), VIDEO, "t1"))
    asyncio.run(send_reply(_Db(replies), CHANNEL, poster, thread, VIDEO, "hello", None))  # type: ignore[arg-type]
    ((query, update, upsert),) = replies.upserts
    assert query == {"channel_id": "ch", "comment_id": "t1"} and upsert is True
    assert update["$set"]["status"] == "replied" and update["$set"]["manual"] is True
    assert update["$set"]["reply_text"] == "hello" and update["$set"]["reply_id"] == "new-reply"


def test_a_blank_reply_is_refused_before_anything_is_posted():
    poster, replies = _Poster(), _Replies()
    thread = asyncio.run(load_thread(CHANNEL, _Reader(), VIDEO, "t1"))
    with pytest.raises(ValueError):
        asyncio.run(send_reply(_Db(replies), CHANNEL, poster, thread, VIDEO, "   ", None))  # type: ignore[arg-type]
    assert poster.sent == [] and replies.upserts == []


def test_a_platform_failure_records_nothing_so_the_thread_still_shows_as_unanswered():
    class _Failing:
        def reply_to_comment(self, comment_id: str, text: str) -> str:
            raise RuntimeError("boom")

    replies = _Replies()
    thread = asyncio.run(load_thread(CHANNEL, _Reader(), VIDEO, "t1"))
    with pytest.raises(RuntimeError):
        asyncio.run(send_reply(_Db(replies), CHANNEL, _Failing(), thread, VIDEO, "hi", None))  # type: ignore[arg-type]
    assert replies.upserts == []


# --- the HTTP routes ----------------------------------------------------------

client = TestClient(app)
HEADERS = {"x-api-key": GLOBAL_API_KEY}
BASE = "/api/v1/channels/ch/comment-inbox"


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"reader": _Reader(), "poster": _Poster(), "gemini": _Gemini("it does")}

    async def channel_and_video(db: Any, channel_id: str, video_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return dict(CHANNEL), dict(VIDEO)

    async def service(channel: dict[str, Any], prefer: str) -> Any:
        return state["poster"] if prefer == "instagram" else state["reader"]

    monkeypatch.setattr(inbox_router, "_channel_and_video", channel_and_video)
    monkeypatch.setattr(inbox_router, "_service", service)
    monkeypatch.setattr("app.main.gemini_service", state["gemini"], raising=False)
    app.dependency_overrides[inbox_router.get_db] = lambda: _Db()
    yield state
    app.dependency_overrides.pop(inbox_router.get_db, None)


def test_the_threads_route_returns_shaped_threads(wired: dict[str, Any]):
    body = client.get(f"{BASE}/threads", params={"video_id": "v1"}, headers=HEADERS).json()
    assert body["video_title"] == "A* vs Dijkstra" and body["threads"][0]["needs_reply"] is True
    assert body["threads"][0]["replies"][1]["author"] == "fan"


def test_the_draft_route_returns_text_and_the_message_it_answers_without_posting(wired: dict[str, Any]):
    response = client.post(f"{BASE}/threads/t1/draft", json={"video_id": "v1"}, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json() == {"text": "@fan it does", "target_comment_id": "r2"}
    assert wired["poster"].sent == []


def test_the_reply_route_posts_under_the_thread(wired: dict[str, Any]):
    response = client.post(
        f"{BASE}/threads/t1/reply",
        json={"video_id": "v1", "text": "it does", "target_comment_id": "r2"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    assert wired["poster"].sent == [("t1", "@fan it does")]


def test_a_blank_reply_is_a_422_not_a_server_error(wired: dict[str, Any]):
    response = client.post(f"{BASE}/threads/t1/reply", json={"video_id": "v1", "text": " "}, headers=HEADERS)
    assert response.status_code == 422


def test_the_routes_need_the_api_key():
    assert client.get(f"{BASE}/threads", params={"video_id": "v1"}).status_code == 401


# --- the platform readers -----------------------------------------------------


class _Request:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


class _YtResource:
    """Stands in for ``youtube.commentThreads()`` / ``youtube.comments()``: replays canned pages, records calls."""

    def __init__(self, pages: list[dict[str, Any]], log: list[dict[str, Any]]) -> None:
        self._pages, self._log = pages, log  # shared across calls: the client builds a new resource each time

    def list(self, **kwargs: Any) -> _Request:
        self._log.append(kwargs)
        return _Request(self._pages.pop(0))


class _YtClient:
    def __init__(self, threads: list[dict[str, Any]], comments: list[dict[str, Any]]) -> None:
        self.thread_calls: list[dict[str, Any]] = []
        self.comment_calls: list[dict[str, Any]] = []
        self._threads, self._comments = threads, comments

    def commentThreads(self) -> _YtResource:  # noqa: N802 — mirrors the Google client's API
        return _YtResource(self._threads, self.thread_calls)

    def comments(self) -> _YtResource:
        return _YtResource(self._comments, self.comment_calls)


def _yt_comment(cid: str, author: str, text: str, at: str, channel: str = "UC-fan") -> dict[str, Any]:
    return {
        "id": cid,
        "snippet": {
            "textDisplay": text,
            "authorDisplayName": author,
            "authorChannelId": {"value": channel},
            "publishedAt": at,
            "likeCount": 2,
            "authorProfileImageUrl": f"https://img/{author}",
        },
    }


def _yt_service(client: _YtClient) -> Any:
    from app.services.youtube import YouTubeService

    service = object.__new__(YouTubeService)  # skips OAuth; only the thread readers are exercised
    service._youtube = client  # type: ignore[attr-defined]
    service._execute = lambda request: request.payload  # type: ignore[attr-defined,method-assign]
    return service


def test_a_youtube_thread_whose_replies_fit_inline_costs_no_extra_request():
    top = _yt_comment("t1", "Fan", "great video", "2026-10-10T01:00:00Z")
    page = {
        "items": [
            {
                "snippet": {"topLevelComment": top, "totalReplyCount": 1},
                "replies": {"comments": [_yt_comment("r1", "Me", "thanks", "2026-10-10T02:00:00Z", "UC-me")]},
            }
        ]
    }
    client = _YtClient([page], [])
    (thread,) = _yt_service(client).get_video_threads("vid", 10)
    assert [r["comment_id"] for r in thread["replies"]] == ["r1"] and client.comment_calls == []
    assert thread["top"]["avatar_url"] == "https://img/Fan" and thread["replies"][0]["author_channel_id"] == "UC-me"


def test_a_youtube_thread_with_more_replies_than_come_inline_is_read_in_full():
    """The listing inlines at most five; showing a conversation cut off would make drafts answer the wrong message."""
    top = _yt_comment("t1", "Fan", "q", "2026-10-10T01:00:00Z")
    inline = [_yt_comment(f"r{i}", "Fan", "x", f"2026-10-10T0{i}:00:00Z") for i in range(1, 6)]
    page = {"items": [{"snippet": {"topLevelComment": top, "totalReplyCount": 7}, "replies": {"comments": inline}}]}
    full = {"items": [_yt_comment(f"r{i}", "Fan", "x", f"2026-10-10T0{i}:00:00Z") for i in range(1, 8)]}
    client = _YtClient([page], [full])
    (thread,) = _yt_service(client).get_video_threads("vid", 10)
    assert len(thread["replies"]) == 7
    assert client.comment_calls[0]["parentId"] == "t1"


def test_a_single_youtube_thread_is_read_by_comment_id_with_all_its_replies():
    client = _YtClient(
        [],
        [
            {"items": [_yt_comment("t1", "Fan", "q", "2026-10-10T01:00:00Z")]},
            {"items": [_yt_comment("r1", "Me", "a", "2026-10-10T02:00:00Z")]},
        ],
    )
    thread = _yt_service(client).get_thread("t1", "vid")
    assert thread["top"]["comment_id"] == "t1" and [r["comment_id"] for r in thread["replies"]] == ["r1"]
    assert thread["comment_url"].endswith("&lc=t1")


def test_a_youtube_comment_that_no_longer_exists_raises_so_the_inbox_can_say_so():
    with pytest.raises(ValueError):
        _yt_service(_YtClient([], [{"items": []}])).get_thread("gone")
