"""The DM setup routes: rich sends, media upload, and ice breakers + menu saved, then published on request."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import instagram_dm_setup as setup_mod
from app.routers import instagram_dms as dms_mod
from tests.conftest import GLOBAL_API_KEY
from tests.mongo_fakes import DB

THEM = "12345678"
BASE = "/api/v1/channels/ch/instagram/dm-setup"
HEADERS = {"x-api-key": GLOBAL_API_KEY}


class _Service:
    provider = "instagram"

    def __init__(self) -> None:
        self.profile_posts: list[dict[str, Any]] = []
        self.cleared: list[list[str]] = []
        self.sent: list[dict[str, Any]] = []

    def send_message(self, ig_user_id: str, recipient_id: str, message: dict[str, Any]) -> str:
        self.sent.append(message)
        return f"mid.{len(self.sent)}"

    def set_messenger_profile(self, ig_user_id: str, body: dict[str, Any]) -> bool:
        self.profile_posts.append(body)
        return True

    def clear_messenger_profile(self, ig_user_id: str, fields: list[str]) -> bool:
        self.cleared.append(fields)
        return True

    def get_messenger_profile(self, ig_user_id: str) -> dict[str, Any]:
        return {"ice_breakers": [{"call_to_actions": [], "locale": "default"}]}


class _Manager:
    def __init__(self, service: _Service) -> None:
        self.service = service

    async def get_service_and_user_id(self, channel_id: str, prefer: str | None = None):
        return self.service, "1"


def _wire(monkeypatch: pytest.MonkeyPatch, service: _Service, db: DB) -> TestClient:
    monkeypatch.setattr("app.main.instagram_service_manager", _Manager(service), raising=False)
    app.dependency_overrides[setup_mod.get_db] = lambda: db
    app.dependency_overrides[dms_mod.get_db] = lambda: db
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_overrides():
    yield
    app.dependency_overrides.pop(setup_mod.get_db, None)
    app.dependency_overrides.pop(dms_mod.get_db, None)


def _db() -> DB:
    return DB(channels=[{"channel_id": "ch", "platform": "instagram", "instagram_username": "tryalgoviz"}])


SETTINGS = {
    "ice_breakers": [{"question": "What is this?", "reply": {"text": "Algorithm visuals"}}],
    "menu": [],
}

SETTINGS_BOTH = {**SETTINGS, "menu": [{"title": "Website", "type": "url", "url": "https://tryalgoviz.com"}]}


def test_saving_settings_does_not_touch_instagram(monkeypatch: pytest.MonkeyPatch) -> None:
    service, db = _Service(), _db()
    client = _wire(monkeypatch, service, db)
    saved = client.put(f"{BASE}/settings", headers=HEADERS, json=SETTINGS)
    assert saved.status_code == 200 and saved.json()["published_at"] is None
    assert service.profile_posts == [] and service.cleared == []
    again = client.get(f"{BASE}/settings", headers=HEADERS).json()
    assert again["settings"]["ice_breakers"][0]["question"] == "What is this?"


def test_publishing_sets_what_is_saved_and_clears_what_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    service, db = _Service(), _db()
    client = _wire(monkeypatch, service, db)
    client.put(f"{BASE}/settings", headers=HEADERS, json=SETTINGS)
    published = client.post(f"{BASE}/settings/publish", headers=HEADERS)
    assert published.status_code == 200 and published.json()["ice_breakers"] is True
    [body] = service.profile_posts
    assert body["ice_breakers"][0]["call_to_actions"][0]["question"] == "What is this?"
    assert service.cleared == [["persistent_menu"]]
    # The ice breaker's reply is registered, so a tap the moment it goes live is answered.
    assert [r["message"]["text"] for r in db.dm_replies.docs] == ["Algorithm visuals"]


def test_nothing_saved_is_nothing_to_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _Service()
    result = _wire(monkeypatch, service, _db()).post(f"{BASE}/settings/publish", headers=HEADERS)
    assert result.status_code == 409 and service.profile_posts == []


def test_too_many_ice_breakers_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    many = {"ice_breakers": SETTINGS["ice_breakers"] * 5, "menu": []}
    result = _wire(monkeypatch, _Service(), _db()).put(f"{BASE}/settings", headers=HEADERS, json=many)
    assert result.status_code == 422


def test_a_rich_send_goes_out_in_parts_and_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    service, db = _Service(), _db()
    message = {
        "media": {"type": "sticker"},
        "text": "Thanks!",
        "buttons": [{"type": "profile", "title": "Our profile"}],
    }
    result = _wire(monkeypatch, service, db).post(
        "/api/v1/channels/ch/instagram/dms/send", headers=HEADERS, json={"recipient_id": THEM, "message": message}
    )
    assert result.status_code == 200 and result.json()["message_ids"] == ["mid.1", "mid.2"]
    assert service.sent[0] == {"attachment": {"type": "like_heart"}}
    assert service.sent[1]["attachment"]["payload"]["buttons"][0]["url"] == "https://www.instagram.com/tryalgoviz/"
    assert [m["text"] for m in db.dm_messages.docs] == ["Thanks!"]


@pytest.mark.parametrize(
    "body",
    [{"recipient_id": THEM}, {"recipient_id": THEM, "text": "hi", "message": {"text": "hi"}}],
)
def test_a_send_is_text_or_a_message_not_both_or_neither(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> None:
    service = _Service()
    result = _wire(monkeypatch, service, _db()).post(
        "/api/v1/channels/ch/instagram/dms/send", headers=HEADERS, json=body
    )
    assert result.status_code == 422 and service.sent == []


def test_an_upload_needs_storage_and_a_type_instagram_takes(monkeypatch: pytest.MonkeyPatch) -> None:
    class _R2:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def upload_video(self, stream: Any, key: str) -> None:
            self.keys.append(key)

        def generate_presigned_url(self, key: str, expires_in: int = 3600, response_content_type: str | None = None):
            return f"https://signed/{key}?type={response_content_type}"

    r2 = _R2()
    client = _wire(monkeypatch, _Service(), _db())
    monkeypatch.setattr("app.main.r2_service", None, raising=False)
    no_storage = client.post(f"{BASE}/media", headers=HEADERS, files={"file": ("a.png", b"x", "image/png")})
    assert no_storage.status_code == 503
    monkeypatch.setattr("app.main.r2_service", r2, raising=False)
    wrong = client.post(f"{BASE}/media", headers=HEADERS, files={"file": ("a.pdf", b"x", "application/pdf")})
    assert wrong.status_code == 415
    monkeypatch.setattr(setup_mod, "MAX_BYTES", {**setup_mod.MAX_BYTES, "image": 4})
    big = client.post(f"{BASE}/media", headers=HEADERS, files={"file": ("a.png", b"12345", "image/png")})
    assert big.status_code == 413 and r2.keys == []
    ok = client.post(f"{BASE}/media", headers=HEADERS, files={"file": ("a.png", b"1234", "image/png")}).json()
    assert ok["type"] == "image" and ok["r2_key"].startswith("dm-media/ch/") and ok["r2_key"].endswith(".png")
    assert ok["preview_url"].endswith("type=image/png") and r2.keys == [ok["r2_key"]]


class _DropsIceBreakersOnMenu(_Service):
    """Instagram's observed fault: the menu post wipes the ice breakers until they are posted again."""

    def __init__(self) -> None:
        super().__init__()
        self.live: dict[str, Any] = {}

    def set_messenger_profile(self, ig_user_id: str, body: dict[str, Any]) -> bool:
        self.profile_posts.append(body)
        if "persistent_menu" in body:
            self.live.pop("ice_breakers", None)
        self.live.update(body)
        return True

    def clear_messenger_profile(self, ig_user_id: str, fields: list[str]) -> bool:
        self.cleared.append(fields)
        for f in fields:
            self.live.pop(f, None)
        return True

    def get_messenger_profile(self, ig_user_id: str) -> dict[str, Any]:
        return dict(self.live)


class _NeverGoesLive(_Service):
    def get_messenger_profile(self, ig_user_id: str) -> dict[str, Any]:
        return {}


def test_ice_breakers_lost_to_the_menu_post_are_set_again(monkeypatch: pytest.MonkeyPatch) -> None:
    service, db = _DropsIceBreakersOnMenu(), _db()
    client = _wire(monkeypatch, service, db)
    client.put(f"{BASE}/settings", headers=HEADERS, json=SETTINGS_BOTH)
    assert client.post(f"{BASE}/settings/publish", headers=HEADERS).status_code == 200
    assert "ice_breakers" in service.live and "persistent_menu" in service.live


def test_publish_fails_loudly_when_nothing_reads_back(monkeypatch: pytest.MonkeyPatch) -> None:
    service, db = _NeverGoesLive(), _db()
    client = _wire(monkeypatch, service, db)
    client.put(f"{BASE}/settings", headers=HEADERS, json=SETTINGS)
    result = client.post(f"{BASE}/settings/publish", headers=HEADERS)
    assert result.status_code == 502 and "ice breakers" in result.json()["detail"]
    assert db.channels.docs[0].get("dm_inbox_published_at") is None


def test_the_live_profile_is_read_one_field_per_call() -> None:
    """Asked for both fields at once Instagram returns only the menu, so each is read on its own."""
    from app.services.instagram import InstagramService

    asked: list[str] = []

    def fake_get(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        asked.append(params["fields"])
        return {"data": [{params["fields"]: ["x"]}]}

    service = object.__new__(InstagramService)
    service._get = fake_get  # type: ignore[method-assign]  # noqa: SLF001
    service._user_node = lambda ig_user_id: ig_user_id  # type: ignore[method-assign]  # noqa: SLF001
    assert service.get_messenger_profile("1") == {"ice_breakers": ["x"], "persistent_menu": ["x"]}
    assert asked == ["ice_breakers", "persistent_menu"]
