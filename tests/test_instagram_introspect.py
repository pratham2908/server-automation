"""Unit cover for live Instagram token introspection.

``introspect_token`` calls Graph ``debug_token`` to learn a token's real
validity/expiry, replacing the old status endpoint's reliance on a stored (and
provably stale) ``expires_at``. These map each Graph response shape without
touching the network.
"""

from __future__ import annotations

import pytest

from app.services import instagram
from app.services.instagram import introspect_token


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
