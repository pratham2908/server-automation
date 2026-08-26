"""The external creator-app API is gone, and must stay gone.

It let a third-party application list, schedule and publish videos using a
per-channel ``X-Channel-Api-Key``. The Import feature replaced it, so the
surface and its credential were removed rather than left dormant.

These tests are the tripwire: re-adding any part of it fails here, which is
where someone will read why it was removed.
"""

from __future__ import annotations

from app import dependencies
from app.main import app
from app.models.channel import Channel

# The server-wide key the analyzer sends as x-api-key, and the JWT login. Both
# share the word "api key" with the deleted credential and neither is affected.
_KEPT_AUTH = ["verify_api_key", "verify_api_key_flexible", "get_current_profile"]

_REMOVED_AUTH = [
    "verify_channel_api_key",
    "generate_channel_api_key",
    "hash_channel_api_key",
    "CHANNEL_API_KEY_PREFIX_LENGTH",
]


def test_no_route_serves_the_external_api():
    offenders = [r.path for r in app.routes if "/api/v1/ext" in getattr(r, "path", "")]
    assert offenders == [], f"external routes are back: {offenders}"


def test_no_route_mints_or_revokes_a_channel_key():
    offenders = [r.path for r in app.routes if getattr(r, "path", "").endswith("/api-key")]
    assert offenders == [], f"channel key management is back: {offenders}"


def test_channel_model_stores_no_key_material():
    leaked = [f for f in Channel.model_fields if f.startswith("api_key")]
    assert leaked == [], f"channel key fields are back on the model: {leaked}"


def test_the_channel_key_dependency_is_gone():
    present = [name for name in _REMOVED_AUTH if hasattr(dependencies, name)]
    assert present == [], f"channel key auth is back: {present}"


def test_the_analyzers_own_auth_still_exists():
    """The removal must not have taken the server-wide key or the login with it."""
    missing = [name for name in _KEPT_AUTH if not hasattr(dependencies, name)]
    assert missing == [], f"removal cut too deep — lost: {missing}"
