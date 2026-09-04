"""Registering an Instagram channel must respect which login the token came from.

Two different APIs sit behind the word "Instagram": the Instagram Graph API via
Facebook Login (graph.facebook.com, addressed by the IG business id) and the
Instagram API with Instagram Login (graph.instagram.com, addressed as `me`).
A token for one returns 401 against the other.

Registration always assumed Facebook Login and never stored the choice, so an
Instagram-Login account could not be registered at all — and had it succeeded,
every later call would still have read `provider` as facebook and used the
wrong host.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import channels
from app.services import instagram


class FakeChannels:
    def __init__(self):
        self.inserted: dict | None = None

    async def find_one(self, _query, _projection=None):
        return None

    async def insert_one(self, doc):
        # Snapshot: the route pops instagram_tokens off the doc before returning
        # it, which would erase the very thing under test.
        self.inserted = copy.deepcopy(doc)
        doc["_id"] = "generated"


class FakeDB:
    def __init__(self):
        self.channels = FakeChannels()


def _body(provider: str) -> SimpleNamespace:
    return SimpleNamespace(
        instagram_user_id="17841418776716682",
        access_token="  tok  ",
        instagram_provider=provider,
        expires_at=None,
        channel_id="geo-ig",
    )


@pytest.fixture
def captured(monkeypatch):
    """Capture how InstagramService was constructed, without any network."""
    seen: dict = {}

    class FakeService:
        def __init__(self, access_token, *, provider=instagram.PROVIDER_FACEBOOK, **_kw):
            seen["token"] = access_token
            seen["provider"] = provider

        def get_account_info(self, ig_user_id):
            seen["asked_for"] = ig_user_id
            return {
                "instagram_user_id": "ig-login-id" if seen["provider"] == "instagram" else ig_user_id,
                "username": "geo",
                "name": "Geo Ranking",
                "biography": "",
                "profile_picture_url": "",
                "followers_count": 10,
                "media_count": 2,
            }

    monkeypatch.setattr(instagram, "InstagramService", FakeService)
    return seen


@pytest.mark.asyncio
async def test_instagram_login_uses_the_instagram_graph_host(captured):
    db = FakeDB()
    await channels._create_instagram_channel(_body("instagram"), db, "p1")
    assert captured["provider"] == instagram.PROVIDER_INSTAGRAM


@pytest.mark.asyncio
async def test_facebook_login_remains_the_default(captured):
    db = FakeDB()
    await channels._create_instagram_channel(_body("facebook"), db, "p1")
    assert captured["provider"] == instagram.PROVIDER_FACEBOOK


@pytest.mark.asyncio
async def test_an_unknown_provider_falls_back_to_facebook(captured):
    """Never send an unrecognised value onward as if it meant something."""
    db = FakeDB()
    await channels._create_instagram_channel(_body("threads"), db, "p1")
    assert captured["provider"] == instagram.PROVIDER_FACEBOOK


@pytest.mark.asyncio
async def test_the_provider_is_stored_on_the_token(captured):
    """Everything downstream reads tokens["provider"] to choose its Graph host."""
    db = FakeDB()
    await channels._create_instagram_channel(_body("instagram"), db, "p1")
    assert db.channels.inserted["instagram_tokens"]["provider"] == "instagram"
    assert db.channels.inserted["instagram_tokens"]["access_token"] == "tok"  # whitespace stripped


@pytest.mark.asyncio
async def test_the_id_the_api_confirms_wins_over_the_pasted_one(captured):
    """Instagram-Login ids are in a different namespace than the FB business id."""
    db = FakeDB()
    await channels._create_instagram_channel(_body("instagram"), db, "p1")
    assert db.channels.inserted["instagram_user_id"] == "ig-login-id"


@pytest.mark.asyncio
async def test_a_failed_lookup_names_the_provider_it_tried(monkeypatch):
    """A bare 401 says nothing; the fix is almost always the other login type."""

    class Failing:
        def __init__(self, *_a, **_kw):
            pass

        def get_account_info(self, _id):
            raise RuntimeError("401 Client Error: Unauthorized")

    monkeypatch.setattr(instagram, "InstagramService", Failing)
    with pytest.raises(HTTPException) as exc:
        await channels._create_instagram_channel(_body("facebook"), FakeDB(), "p1")
    assert "Facebook Login" in exc.value.detail
    assert "Instagram Login" in exc.value.detail  # the suggested alternative
