"""Channel API key system — model fields, auth dependency, and management endpoints."""

import hashlib

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.dependencies import hash_channel_api_key, verify_channel_api_key
from app.models.channel import Channel

# ------------------------------------------------------------------
# Channel model
# ------------------------------------------------------------------


def test_channel_defaults_to_no_api_key():
    """A channel built without key fields is valid and reports no key."""
    channel = Channel(channel_id="histriphy", name="Histriphy")

    assert channel.api_key_hash is None
    assert channel.api_key_prefix is None
    assert channel.api_key_created_at is None


def test_channel_accepts_api_key_fields():
    """Existing channel documents carrying key fields load without migration errors."""
    channel = Channel(
        channel_id="histriphy",
        name="Histriphy",
        api_key_hash="a" * 64,
        api_key_prefix="ckey_histri",
        api_key_created_at="2026-07-24T10:00:00+05:30",
    )

    assert channel.api_key_hash == "a" * 64
    assert channel.api_key_prefix == "ckey_histri"
    assert channel.api_key_created_at.year == 2026


# ------------------------------------------------------------------
# verify_channel_api_key dependency
# ------------------------------------------------------------------


def test_hash_channel_api_key_is_sha256_hex():
    assert hash_channel_api_key("ckey_histriphy_abc") == hashlib.sha256(b"ckey_histriphy_abc").hexdigest()


@pytest.fixture
def probe_client(fake_db):
    """A throwaway app whose single route is guarded by verify_channel_api_key."""
    probe = FastAPI()

    @probe.get("/probe/{channel_id}")
    async def probe_route(verified_channel_id: str = Depends(verify_channel_api_key)):
        return {"channel_id": verified_channel_id}

    probe.dependency_overrides[get_db] = lambda: fake_db
    return TestClient(probe)


def _set_key(fake_db, channel_id: str, raw_key: str) -> None:
    fake_db.channels.raw(channel_id)["api_key_hash"] = hashlib.sha256(raw_key.encode()).hexdigest()


def test_valid_key_passes_and_returns_channel_id(probe_client, fake_db):
    _set_key(fake_db, "histriphy", "ckey_histriphy_secret")

    response = probe_client.get("/probe/histriphy", headers={"X-Channel-Api-Key": "ckey_histriphy_secret"})

    assert response.status_code == 200
    assert response.json() == {"channel_id": "histriphy"}


def test_missing_header_is_rejected(probe_client, fake_db):
    _set_key(fake_db, "histriphy", "ckey_histriphy_secret")

    assert probe_client.get("/probe/histriphy").status_code == 401


def test_wrong_key_is_rejected(probe_client, fake_db):
    _set_key(fake_db, "histriphy", "ckey_histriphy_secret")

    response = probe_client.get("/probe/histriphy", headers={"X-Channel-Api-Key": "ckey_histriphy_wrong"})

    assert response.status_code == 401


def test_channel_without_a_key_rejects_every_request(probe_client):
    """`otherchan` has no key set — no header value can authenticate it."""
    response = probe_client.get("/probe/otherchan", headers={"X-Channel-Api-Key": "ckey_otherchan_anything"})

    assert response.status_code == 401


def test_key_for_one_channel_does_not_unlock_another(probe_client, fake_db):
    _set_key(fake_db, "histriphy", "ckey_histriphy_secret")
    _set_key(fake_db, "otherchan", "ckey_otherchan_secret")

    response = probe_client.get("/probe/otherchan", headers={"X-Channel-Api-Key": "ckey_histriphy_secret"})

    assert response.status_code == 401


def test_unknown_channel_is_rejected_without_disclosing_existence(probe_client):
    response = probe_client.get("/probe/nosuchchannel", headers={"X-Channel-Api-Key": "ckey_nosuchchannel_x"})

    assert response.status_code == 401


# ------------------------------------------------------------------
# POST /api/v1/channels/{channel_id}/api-key  —  generate / rotate
# ------------------------------------------------------------------


def test_generate_returns_raw_key_and_stores_only_the_hash(client, fake_db, auth_headers):
    response = client.post("/api/v1/channels/histriphy/api-key", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    raw_key = body["raw_key"]
    assert raw_key.startswith("ckey_histriphy_")
    assert body["prefix"] == raw_key[:12]
    assert body["created_at"]

    stored = fake_db.channels.raw("histriphy")
    assert stored["api_key_hash"] == hashlib.sha256(raw_key.encode()).hexdigest()
    assert raw_key not in str(stored)


def test_rotating_issues_a_new_key_and_invalidates_the_old_hash(client, fake_db, auth_headers):
    first = client.post("/api/v1/channels/histriphy/api-key", headers=auth_headers).json()["raw_key"]
    first_hash = fake_db.channels.raw("histriphy")["api_key_hash"]

    second = client.post("/api/v1/channels/histriphy/api-key", headers=auth_headers).json()["raw_key"]

    assert second != first
    assert fake_db.channels.raw("histriphy")["api_key_hash"] != first_hash


def test_generate_on_unknown_channel_is_404(client, auth_headers):
    assert client.post("/api/v1/channels/nosuchchannel/api-key", headers=auth_headers).status_code == 404


# ------------------------------------------------------------------
# GET /api/v1/channels/{channel_id}/api-key  —  metadata only
# ------------------------------------------------------------------


def test_key_info_reports_absent_key(client, auth_headers):
    response = client.get("/api/v1/channels/histriphy/api-key", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"has_key": False, "prefix": None, "created_at": None}


def test_key_info_never_exposes_raw_key_or_hash(client, auth_headers):
    raw_key = client.post("/api/v1/channels/histriphy/api-key", headers=auth_headers).json()["raw_key"]

    response = client.get("/api/v1/channels/histriphy/api-key", headers=auth_headers)

    body = response.json()
    assert body["has_key"] is True
    assert body["prefix"] == raw_key[:12]
    assert body["created_at"]
    assert set(body) == {"has_key", "prefix", "created_at"}
    serialized = response.text
    assert raw_key not in serialized
    assert hashlib.sha256(raw_key.encode()).hexdigest() not in serialized


# ------------------------------------------------------------------
# DELETE /api/v1/channels/{channel_id}/api-key  —  revoke
# ------------------------------------------------------------------


def test_revoke_clears_all_three_fields(client, fake_db, auth_headers):
    client.post("/api/v1/channels/histriphy/api-key", headers=auth_headers)

    response = client.delete("/api/v1/channels/histriphy/api-key", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    stored = fake_db.channels.raw("histriphy")
    assert stored["api_key_hash"] is None
    assert stored["api_key_prefix"] is None
    assert stored["api_key_created_at"] is None


def test_revoked_key_no_longer_authenticates(client, fake_db, auth_headers, probe_client):
    raw_key = client.post("/api/v1/channels/histriphy/api-key", headers=auth_headers).json()["raw_key"]
    assert probe_client.get("/probe/histriphy", headers={"X-Channel-Api-Key": raw_key}).status_code == 200

    client.delete("/api/v1/channels/histriphy/api-key", headers=auth_headers)

    assert probe_client.get("/probe/histriphy", headers={"X-Channel-Api-Key": raw_key}).status_code == 401


# ------------------------------------------------------------------
# Global API key protects all three management endpoints
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    ["post", "get", "delete"],
)
def test_key_management_requires_the_global_api_key(client, method):
    call = getattr(client, method)

    assert call("/api/v1/channels/histriphy/api-key").status_code == 401
    assert call("/api/v1/channels/histriphy/api-key", headers={"X-API-Key": "wrong"}).status_code == 401
