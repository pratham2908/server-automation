"""Brand lookups: handle clean-up, query, shaping, cache, and the route's handling of Instagram's refusals."""

from __future__ import annotations

from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.routers import brands as router_mod
from app.services import brand_lookup as bl
from tests.conftest import GLOBAL_API_KEY
from tests.mongo_fakes import DB

REPLY = {
    "business_discovery": {
        "username": "acme",
        "name": "Acme",
        "biography": "hi@acme.com",
        "website": "https://acme.com",
        "followers_count": 1200,
        "media_count": 80,
        "media": {
            "data": [
                {
                    "caption": "#ad with @creator",
                    "permalink": "https://instagram.com/p/1",
                    "timestamp": "2026-11-01T10:00:00+0000",
                    "comments_count": 4,
                    "media_type": "IMAGE",
                }
            ]
        },
    }
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@Acme", "acme"),
        (" acme.co ", "acme.co"),
        ("https://www.instagram.com/Acme/?hl=en", "acme"),
        ("not a handle", None),
        ("", None),
    ],
)
def test_clean_handle(raw: str, expected: str | None) -> None:
    assert bl.clean_handle(raw) == expected


def test_fields_ask_for_profile_and_media_in_one_call() -> None:
    fields = bl.discovery_fields("acme", 30)
    assert fields.startswith("business_discovery.username(acme){") and "media.limit(30){" in fields


def test_shape_keeps_hidden_likes_as_none() -> None:
    out = bl.shape("acme", REPLY)
    assert out["followers_count"] == 1200 and out["media"][0]["like_count"] is None


def test_cache_keeps_successes_for_a_day_and_not_failures() -> None:
    now = [0.0]
    cache = bl.LookupCache(clock=lambda: now[0], ttl=100)
    cache.put("a", 30, bl.shape("a", REPLY))
    cache.put("b", 30, bl.failure("b", "error", "x"))
    assert cache.get("a", 30) is not None and cache.get("b", 30) is None
    now[0] = 101
    assert cache.get("a", 30) is None


class _Service:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def business_discovery(self, ig_user_id: str, fields: str) -> dict[str, Any]:
        self.calls.append(fields)
        if "limited" in fields:
            resp = requests.Response()
            resp.status_code = 403
            resp._content = b'{"error": {"message": "(#4) Application request limit reached", "code": 4}}'
            raise requests.HTTPError(response=resp)
        if "personal" in fields:
            resp = requests.Response()
            resp.status_code = 400
            resp._content = b'{"error": {"message": "Invalid user id", "code": 110}}'
            raise requests.HTTPError(response=resp)
        return REPLY


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _Service]:
    service = _Service()
    db = DB(channels=[{"channel_id": "DreamScenicAi"}])

    async def fake_service(_db: Any, _channel: str) -> tuple[_Service, str]:
        return service, "123"

    monkeypatch.setattr(router_mod, "_service", fake_service)
    monkeypatch.setattr(router_mod, "_cache", bl.LookupCache())
    monkeypatch.setattr(router_mod, "PACE_SECONDS", 0)
    monkeypatch.setattr(router_mod, "_guard", bl.CallGuard())
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app, headers={"X-API-Key": GLOBAL_API_KEY}), service
    app.dependency_overrides.pop(get_db, None)


def test_get_brand_then_cached(client: tuple[TestClient, _Service]) -> None:
    http, service = client
    first = http.get("/api/v1/channels/DreamScenicAi/brands/@Acme").json()
    second = http.get("/api/v1/channels/DreamScenicAi/brands/acme").json()
    assert first["status"] == "ok" and first["cached"] is False and second["cached"] is True
    assert len(service.calls) == 1


def test_batch_reports_each_brand_in_order(client: tuple[TestClient, _Service]) -> None:
    http, service = client
    body = {"handles": ["acme", "personal.account", "bad handle"], "media_limit": 5}
    out = http.post("/api/v1/channels/DreamScenicAi/brands/batch", json=body).json()
    assert [o["status"] for o in out] == ["ok", "not_business", "invalid_handle"]
    assert out[1]["error"] and len(service.calls) == 2


def test_app_limit_pauses_later_lookups_without_calling_graph(client: tuple[TestClient, _Service]) -> None:
    http, service = client
    out = http.post("/api/v1/channels/DreamScenicAi/brands/batch", json={"handles": ["limited.one", "acme"]}).json()
    assert [o["status"] for o in out] == ["rate_limited", "rate_limited"]
    assert len(service.calls) == 1


def test_guard_caps_live_calls_per_hour() -> None:
    now = [0.0]
    guard = bl.CallGuard(clock=lambda: now[0])
    for _ in range(bl.LIVE_CALLS_PER_HOUR):
        assert guard.refusal() is None
        guard.record_call()
    assert guard.refusal() is not None
    now[0] = 3601
    assert guard.refusal() is None
