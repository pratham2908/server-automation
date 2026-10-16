"""Auto-refresh of a near-expiry token must use the endpoint for its login type."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import instagram as instagram_mod


@pytest.mark.parametrize("provider", ["instagram", "facebook"])
def test_near_expiry_token_is_refreshed_with_its_own_login_type(client, fake_db, auth_headers, monkeypatch, provider):
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    fake_db.channels._docs.append(
        {
            "channel_id": "igchan",
            "name": "IG",
            "platform": "instagram",
            "instagram_tokens": {"access_token": "old-token", "expires_at": soon, "provider": provider},
        }
    )

    async def fake_config(db):
        return {"app_id": "app", "app_secret": "secret"}

    seen: list[str] = []

    async def fake_refresh(self, app_id, app_secret):
        seen.append(self._provider)
        return "new-token"

    monkeypatch.setattr("app.database.get_instagram_oauth_config", fake_config)
    monkeypatch.setattr(instagram_mod.InstagramService, "refresh_token", fake_refresh)
    # The route drops its cached client after a refresh; no real manager in tests.
    monkeypatch.setattr(
        "app.routers.channels._get_instagram_manager", lambda: SimpleNamespace(invalidate=lambda cid: None)
    )

    resp = client.get("/api/v1/channels/igchan/instagram-token", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert seen == [provider]
