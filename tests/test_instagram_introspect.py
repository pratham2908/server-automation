"""Unit cover for live Instagram token introspection.

``introspect_token`` calls Graph ``debug_token`` to learn a token's real
validity/expiry, replacing the old status endpoint's reliance on a stored (and
provably stale) ``expires_at``. These map each Graph response shape without
touching the network.
"""

from __future__ import annotations

import pytest

from app.services import instagram
from app.services.instagram import (
    PROVIDER_FACEBOOK,
    PROVIDER_INSTAGRAM,
    InstagramService,
    _base_for,
    introspect_token,
)


class TestTokenSanitization:
    """A pasted token with a trailing newline must not reach the Bearer header —
    ``Authorization: Bearer <token>\\n`` raises 'Invalid header value' and fails
    every Graph call (observed on Dream Scenic Ai: sync 400 + 5 failed publishes).
    """

    def test_strips_trailing_newline(self):
        assert InstagramService("EAAtoken\n")._token == "EAAtoken"

    def test_strips_surrounding_whitespace(self):
        assert InstagramService("  EAAtoken \n")._token == "EAAtoken"


class TestProviderRouting:
    """Facebook Login and Instagram Login run in parallel; the base host and the
    validity check are chosen per channel by the stored ``provider`` flag."""

    def test_base_for_instagram(self):
        assert "graph.instagram.com" in _base_for(PROVIDER_INSTAGRAM)

    def test_base_for_facebook_is_default(self):
        assert "graph.facebook.com" in _base_for(PROVIDER_FACEBOOK)
        assert "graph.facebook.com" in _base_for("anything-unknown")

    def test_service_uses_provider_base(self):
        assert "graph.instagram.com" in InstagramService("t", provider=PROVIDER_INSTAGRAM)._base
        assert "graph.facebook.com" in InstagramService("t")._base

    def test_business_discovery_blocked_on_instagram(self):
        svc = InstagramService("tok", provider=PROVIDER_INSTAGRAM)
        with pytest.raises(ValueError, match="business_discovery"):
            svc.discover_business_account("ownid", "someuser")


class TestInstagramLoginIntrospect:
    """Instagram-Login tokens validate via graph.instagram.com/me, not debug_token."""

    def test_valid_via_me(self, mock_get):
        mock_get({"id": "17841400000000000", "username": "dreamscenicai"})
        r = introspect_token("tok", provider=PROVIDER_INSTAGRAM)
        assert r.reachable and r.valid and r.expires_at is None

    def test_expired_via_me(self, mock_get):
        mock_get({"error": {"code": 190, "message": "Session has expired"}})
        r = introspect_token("tok", provider=PROVIDER_INSTAGRAM)
        assert r.reachable and not r.valid

    def test_unreachable_via_me(self, mock_get):
        mock_get(exc=OSError("boom"))
        r = introspect_token("tok", provider=PROVIDER_INSTAGRAM)
        assert not r.reachable and not r.valid


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def mock_get(monkeypatch):
    def _install(payload: dict | None = None, exc: Exception | None = None) -> None:
        def fake_get(*args, **kwargs):
            if exc is not None:
                raise exc
            return _FakeResp(payload or {})

        monkeypatch.setattr(instagram.requests, "get", fake_get)

    return _install


class TestIntrospectToken:
    def test_valid_never_expires(self, mock_get):
        """The four shared-token channels: valid, expires_at 0 (never)."""
        mock_get({"data": {"is_valid": True, "expires_at": 0, "type": "USER"}})
        r = introspect_token("tok")
        assert r.reachable and r.valid and r.expires_at == 0

    def test_valid_with_expiry(self, mock_get):
        mock_get({"data": {"is_valid": True, "expires_at": 1893456000}})
        r = introspect_token("tok")
        assert r.reachable and r.valid and r.expires_at == 1893456000

    def test_self_debug_expired_is_invalid(self, mock_get):
        """Self-debugging an expired token returns a top-level OAuthException 190."""
        mock_get({"error": {"code": 190, "error_subcode": 463, "message": "Session has expired"}})
        r = introspect_token("tok")
        assert r.reachable and not r.valid
        assert "expired" in (r.error or "").lower()

    def test_app_token_debug_of_expired(self, mock_get):
        """With app creds, an expired token comes back inside data with is_valid False."""
        mock_get({"data": {"is_valid": False, "error": {"message": "expired"}}})
        r = introspect_token("tok", app_id="1", app_secret="2")
        assert r.reachable and not r.valid

    def test_network_error_is_unreachable(self, mock_get):
        """A transient failure must NOT report the token as invalid."""
        mock_get(exc=OSError("boom"))
        r = introspect_token("tok")
        assert not r.reachable and not r.valid

    def test_other_error_is_inconclusive(self, mock_get):
        """A non-190 error (e.g. rate limit) is inconclusive, not authoritative."""
        mock_get({"error": {"code": 4, "message": "rate limited"}})
        r = introspect_token("tok")
        assert not r.reachable
