"""Reading DMs on request: Meta's objects shaped for the dashboard, and the routes over them."""

from __future__ import annotations

from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient

from app.main import app
from app.routers import instagram_dms as router_mod
from app.services.instagram_dm_inbox import next_cursor, parse_conversation, parse_message, parse_thread
from tests.conftest import GLOBAL_API_KEY

OWN = "tryalgoviz"


def message(
    msg_id: str, sender: str, text: str | None = None, when: str = "2026-10-27T08:00:00+0000", **extra: Any
) -> dict[str, Any]:
    return {"id": msg_id, "created_time": when, "from": {"username": sender, "id": "1"}, "message": text, **extra}


def test_which_side_is_us_comes_from_the_handle_not_the_order() -> None:
    mine = parse_message(message("m1", "TryAlgoViz", "hi"), OWN)
    theirs = parse_message(message("m2", "gdg_rvce", "hello"), OWN)
    assert (mine.from_me, theirs.from_me) == (True, False)


def test_times_are_shown_in_ist() -> None:
    parsed = parse_message(message("m1", "x", "hi", when="2026-10-27T08:00:00+0000"), OWN)
    assert parsed.created_at is not None and parsed.created_at.endswith("+05:30") and "13:30" in parsed.created_at


@pytest.mark.parametrize(
    ("extra", "kind", "link"),
    [
        (
            {"shares": {"data": [{"link": "https://www.instagram.com/reel/abc/"}]}},
            "share",
            "https://www.instagram.com/reel/abc/",
        ),
        ({"story": {"link": "https://cdn.example/s.jpg"}}, "story", "https://cdn.example/s.jpg"),
        (
            {"attachments": {"data": [{"image_data": {"url": "https://cdn.example/i.jpg"}}]}},
            "attachment",
            "https://cdn.example/i.jpg",
        ),
        ({"is_unsupported": True}, "unsupported", None),
        ({}, "text", None),
    ],
)
def test_a_message_says_what_kind_it_is(extra: dict[str, Any], kind: str, link: str | None) -> None:
    parsed = parse_message(message("m", "x", None, **extra), OWN)
    assert (parsed.kind, parsed.link) == (kind, link)


def test_a_conversation_is_named_for_the_other_person_with_its_latest_message() -> None:
    raw = {
        "id": "c1",
        "updated_time": "2026-10-27T08:00:00+0000",
        "participants": {"data": [{"username": OWN, "id": "1"}, {"username": "gdg_rvce", "id": "2"}]},
        "messages": {"data": [message("m1", "gdg_rvce", "hello")]},
    }
    parsed = parse_conversation(raw, OWN)
    assert (parsed.with_username, parsed.with_id) == ("gdg_rvce", "2")
    assert parsed.last_message is not None and parsed.last_message.text == "hello"


def test_a_conversation_with_nothing_in_it_still_parses() -> None:
    parsed = parse_conversation({"id": "c2"}, OWN)
    assert parsed.with_username is None and parsed.last_message is None


def test_a_thread_reads_oldest_first() -> None:
    newest_first = [message("m2", "x", "second"), message("m1", "x", "first")]
    assert [m.text for m in parse_thread(newest_first, OWN)] == ["first", "second"]


def test_a_cursor_is_offered_only_when_there_is_a_next_page() -> None:
    assert next_cursor({"paging": {"cursors": {"after": "abc"}, "next": "https://graph/next"}}) == "abc"
    assert next_cursor({"paging": {"cursors": {"after": "abc"}}}) is None
    assert next_cursor({}) is None


# ---- routes ----


class _Db:
    class channels:  # noqa: N801 - mimics a collection attribute
        @staticmethod
        async def find_one(query: dict[str, Any], projection: Any = None) -> dict[str, Any] | None:
            return {"channel_id": "ch"} if query.get("channel_id") == "ch" else None


class _Service:
    def __init__(self, error: requests.RequestException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[Any, ...]] = []

    def own_username(self, ig_user_id: str) -> str:
        return OWN

    def list_conversations(self, ig_user_id: str, limit: int, after: str | None) -> dict[str, Any]:
        self.calls.append(("list", limit, after))
        if self.error:
            raise self.error
        return {
            "data": [
                {
                    "id": "c1",
                    "updated_time": "2026-10-27T08:00:00+0000",
                    "participants": {"data": [{"username": OWN, "id": "1"}, {"username": "gdg_rvce", "id": "2"}]},
                    "messages": {"data": [message("m1", "gdg_rvce", "hello")]},
                }
            ],
            "paging": {"cursors": {"after": "NEXT"}, "next": "https://graph/next"},
        }

    def get_conversation_messages(self, conversation_id: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append(("thread", conversation_id, limit))
        return [message("m2", OWN, "reply"), message("m1", "gdg_rvce", "hello")]


class _Manager:
    def __init__(self, service: _Service) -> None:
        self.service = service

    async def get_service_and_user_id(self, channel_id: str, prefer: str | None = None) -> tuple[_Service, str]:
        return self.service, "1"


def _wire(monkeypatch: pytest.MonkeyPatch, service: _Service) -> TestClient:
    monkeypatch.setattr("app.main.instagram_service_manager", _Manager(service), raising=False)
    app.dependency_overrides[router_mod.get_db] = _Db
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_overrides():
    yield
    app.dependency_overrides.pop(router_mod.get_db, None)


def _headers() -> dict[str, str]:
    return {"x-api-key": GLOBAL_API_KEY}


def test_listing_asks_instagram_once_and_returns_a_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _Service()
    body = _wire(monkeypatch, service).get("/api/v1/channels/ch/instagram/dms?limit=5", headers=_headers()).json()
    assert service.calls == [("list", 5, None)]
    assert body["conversations"][0]["with_username"] == "gdg_rvce"
    assert body["next_cursor"] == "NEXT"


def test_a_thread_comes_back_oldest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _wire(monkeypatch, _Service())
    body = client.get("/api/v1/channels/ch/instagram/dms/convo_abcdef12", headers=_headers()).json()
    assert [(m["text"], m["from_me"]) for m in body["messages"]] == [("hello", False), ("reply", True)]


def test_a_missing_permission_is_a_clear_403(monkeypatch: pytest.MonkeyPatch) -> None:
    response = requests.Response()
    response.status_code = 400
    response._content = b'{"error": {"message": "(#10) App is missing the `instagram_manage_messages` permission"}}'
    client = _wire(monkeypatch, _Service(requests.HTTPError(response=response)))
    result = client.get("/api/v1/channels/ch/instagram/dms", headers=_headers())
    assert result.status_code == 403 and "permission is missing" in result.json()["detail"]


def test_an_unknown_channel_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        _wire(monkeypatch, _Service()).get("/api/v1/channels/nope/instagram/dms", headers=_headers()).status_code == 404
    )


# ---- sending ----


def _msg(created: str, mine: bool) -> Any:
    from app.services.instagram_dm_inbox import DmMessageOut

    return DmMessageOut(id=created, created_at=created, from_me=mine, text="x")


def test_the_window_runs_from_their_newest_message_not_ours() -> None:
    from datetime import datetime

    from app.services.instagram_dm_inbox import reply_window_ends_at
    from app.timezone import IST

    now = datetime(2026, 10, 28, 12, 0, tzinfo=IST)
    thread = [_msg("2026-10-27T20:00:00+05:30", False), _msg("2026-10-28T11:00:00+05:30", True)]
    assert reply_window_ends_at(thread, now) == "2026-10-28T20:00:00+05:30"


def test_a_window_that_has_passed_or_never_opened_is_none() -> None:
    from datetime import datetime

    from app.services.instagram_dm_inbox import reply_window_ends_at
    from app.timezone import IST

    now = datetime(2026, 10, 28, 12, 0, tzinfo=IST)
    assert reply_window_ends_at([_msg("2026-10-26T20:00:00+05:30", False)], now) is None
    assert reply_window_ends_at([_msg("2026-10-28T11:00:00+05:30", True)], now) is None
    assert reply_window_ends_at([], now) is None


def test_sending_goes_out_only_when_called_and_returns_the_message_id(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _Service()
    sent: list[tuple[str, str, str]] = []

    def send_dm(ig_user_id: str, recipient_id: str, text: str) -> str:
        sent.append((ig_user_id, recipient_id, text))
        return "mid.1"

    service.send_dm = send_dm  # type: ignore[attr-defined]
    client = _wire(monkeypatch, service)
    assert service.calls == [] and sent == []
    result = client.post(
        "/api/v1/channels/ch/instagram/dms/send", headers=_headers(), json={"recipient_id": "12345678", "text": "hi"}
    )
    assert result.json()["message_id"] == "mid.1" and sent == [("1", "12345678", "hi")]


def test_a_closed_window_is_a_409_with_instagrams_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    response = requests.Response()
    response.status_code = 400
    response._content = b'{"error": {"message": "This message is sent outside of allowed window."}}'
    service = _Service()

    def refuse(*_: Any) -> str:
        raise requests.HTTPError(response=response)

    service.send_dm = refuse  # type: ignore[attr-defined]
    result = _wire(monkeypatch, service).post(
        "/api/v1/channels/ch/instagram/dms/send", headers=_headers(), json={"recipient_id": "12345678", "text": "hi"}
    )
    assert result.status_code == 409 and "24 hours" in result.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"recipient_id": "abc", "text": "hi"},
        {"recipient_id": "12345678", "text": ""},
        {"recipient_id": "12345678", "text": "x" * 1001},
    ],
)
def test_a_malformed_send_never_reaches_instagram(monkeypatch: pytest.MonkeyPatch, body: dict[str, str]) -> None:
    service = _Service()
    result = _wire(monkeypatch, service).post("/api/v1/channels/ch/instagram/dms/send", headers=_headers(), json=body)
    assert result.status_code == 422 and service.calls == []
