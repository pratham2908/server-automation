"""A private reply sent by hand to one comment: built like the worker's, recorded like the worker's, sent once."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient

from app.main import app
from app.routers import comment_private_reply as router_mod
from tests.conftest import GLOBAL_API_KEY
from tests.mongo_fakes import DB

COMMENT = "18000000000000001"
URL = f"/api/v1/channels/ch/instagram/comments/{COMMENT}/private-reply"
CARD = {
    "cards": [
        {
            "title": "CitySolved",
            "subtitle": "Your city, solved",
            "image": {"type": "image", "url": "https://cdn.example.com/card.jpg"},
            "buttons": [{"type": "url", "title": "Get early access", "url": "https://citysolved.com/?src=reply"}],
        }
    ]
}


class _Service:
    def __init__(self, error: requests.RequestException | None = None) -> None:
        self.error = error
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    def send_private_reply(self, ig_user_id: str, comment_id: str, message: dict[str, Any]) -> str:
        if self.error:
            raise self.error
        self.sent.append((ig_user_id, comment_id, message))
        return f"mid.{len(self.sent)}"


class _Manager:
    def __init__(self, service: _Service) -> None:
        self.service = service

    async def get_service_and_user_id(self, channel_id: str, prefer: str | None = None) -> tuple[_Service, str]:
        return self.service, "1"


def _db(**extra: list[dict[str, Any]]) -> DB:
    return DB(channels=[{"channel_id": "ch", "platform": "instagram", "instagram_username": "own"}], **extra)


def _wire(monkeypatch: pytest.MonkeyPatch, service: _Service, db: DB) -> TestClient:
    monkeypatch.setattr("app.main.instagram_service_manager", _Manager(service), raising=False)
    app.dependency_overrides[router_mod.get_db] = lambda: db
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_overrides():
    yield
    app.dependency_overrides.pop(router_mod.get_db, None)


def _headers() -> dict[str, str]:
    return {"x-api-key": GLOBAL_API_KEY}


def _refusal(message: str) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = 400
    response._content = json.dumps({"error": {"message": message}}).encode()
    return requests.HTTPError(response=response)


def test_a_card_goes_as_one_generic_template_and_is_recorded_as_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _Service()
    seen = {
        "comment_id": COMMENT,
        "channel_id": "ch",
        "media_id": "m1",
        "username": "Asker",
        "from_id": "777",
        "text": "app link?",
        "commented_at": datetime(2026, 11, 10, tzinfo=UTC),
    }
    db = _db(comment_log=[seen])
    result = _wire(monkeypatch, service, db).post(URL, headers=_headers(), json=CARD)
    assert result.status_code == 200, result.text
    body = result.json()
    assert (body["message_id"], body["dm_format"], body["username"]) == ("mid.1", "cards", "Asker")
    [(ig_user_id, comment_id, message)] = service.sent
    assert (ig_user_id, comment_id) == ("1", COMMENT)
    payload = message["attachment"]["payload"]
    [element] = payload["elements"]
    assert payload["template_type"] == "generic"
    assert element["image_url"] == "https://cdn.example.com/card.jpg"
    assert element["buttons"][0]["url"] == "https://citysolved.com/?src=reply"
    [event] = db.comment_dm_events.docs
    assert (event["status"], event["source"], event["dm_message_id"], event["person"], event["media_id"]) == (
        "sent",
        "manual",
        "mid.1",
        "asker",
        "m1",
    )


def test_a_second_call_for_the_same_comment_is_refused_without_calling_instagram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service()
    client = _wire(monkeypatch, service, _db())
    assert client.post(URL, headers=_headers(), json={"text": "hi"}).status_code == 200
    again = client.post(URL, headers=_headers(), json={"text": "hi"})
    assert again.status_code == 409 and len(service.sent) == 1


def test_a_comment_the_worker_queued_is_taken_over_so_it_gets_one_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    queued = {"comment_id": COMMENT, "channel_id": "ch", "status": "pending", "username": "asker", "attempts": 0}
    db = _db(comment_dm_events=[queued])
    result = _wire(monkeypatch, _Service(), db).post(URL, headers=_headers(), json={"text": "hi"})
    assert result.status_code == 200
    [event] = db.comment_dm_events.docs
    assert event["status"] == "sent" and event["manual"] is True and event["attempts"] == 1


def test_one_in_flight_from_the_worker_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    sending = {"comment_id": COMMENT, "channel_id": "ch", "status": "sending"}
    service = _Service()
    result = _wire(monkeypatch, service, _db(comment_dm_events=[sending])).post(
        URL, headers=_headers(), json={"text": "hi"}
    )
    assert result.status_code == 409 and service.sent == []


def test_a_closed_window_comes_back_as_409_with_instagrams_reason_and_leaves_no_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db()
    service = _Service(_refusal("This message is sent outside of allowed window."))
    result = _wire(monkeypatch, service, db).post(URL, headers=_headers(), json=CARD)
    assert result.status_code == 409 and "outside of allowed window" in result.json()["detail"]
    assert db.comment_dm_events.docs == []


def test_a_refusal_on_a_queued_comment_puts_it_back_as_it_was(monkeypatch: pytest.MonkeyPatch) -> None:
    queued = {"comment_id": COMMENT, "channel_id": "ch", "status": "pending", "attempts": 0}
    db = _db(comment_dm_events=[queued])
    result = _wire(monkeypatch, _Service(_refusal("Invalid parameter")), db).post(URL, headers=_headers(), json=CARD)
    assert result.status_code == 502 and "Invalid parameter" in result.json()["detail"]
    assert db.comment_dm_events.docs[0]["status"] == "pending"


def test_media_with_text_is_two_messages_and_refused_before_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _Service()
    body = {"text": "hi", "media": {"type": "image", "url": "https://cdn.example.com/a.jpg"}}
    result = _wire(monkeypatch, service, _db()).post(URL, headers=_headers(), json=body)
    assert result.status_code == 422 and service.sent == []


def test_needs_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _wire(monkeypatch, _Service(), _db()).post(URL, json={"text": "hi"}).status_code in (401, 403)
