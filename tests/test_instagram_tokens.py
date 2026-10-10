"""A channel holding both an Instagram Login and a Facebook Login token.

The point is that each feature gets the token that actually works for it, that
the Facebook token is addressed by the right account id, and that neither token
can ever leak out of a channel response.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services import instagram as instagram_module
from app.services.competitor_feed import is_business_discovery_capable
from app.services.instagram import InstagramServiceManager, find_business_account_id
from app.services.instagram_tokens import (
    ALT_FIELD,
    PRIMARY_FIELD,
    PROVIDER_FACEBOOK,
    PROVIDER_INSTAGRAM,
    SECRET_CHANNEL_FIELDS,
    build_token_doc,
    has_provider,
    ig_user_id_for,
    secret_projection,
    select_slot,
    slot_for_store,
    summarise_slots,
)


def _both() -> dict[str, Any]:
    """TryAlgoViz's shape: Instagram Login primary, a Facebook token alongside it."""
    return {
        "channel_id": "tav",
        "platform": "instagram",
        "instagram_user_id": "ig-scoped-id",
        PRIMARY_FIELD: {"access_token": "IG-TOKEN", "provider": PROVIDER_INSTAGRAM},
        ALT_FIELD: {"access_token": "FB-TOKEN", "provider": PROVIDER_FACEBOOK, "instagram_user_id": "business-id"},
    }


# --- choosing a token ---------------------------------------------------------


def test_the_preferred_login_type_wins_wherever_it_is_stored():
    field, tokens = select_slot(_both(), PROVIDER_FACEBOOK)  # type: ignore[misc]
    assert field == ALT_FIELD and tokens["access_token"] == "FB-TOKEN"
    field, tokens = select_slot(_both(), PROVIDER_INSTAGRAM)  # type: ignore[misc]
    assert field == PRIMARY_FIELD and tokens["access_token"] == "IG-TOKEN"


def test_no_preference_means_the_primary_so_existing_features_are_unchanged():
    field, _ = select_slot(_both())  # type: ignore[misc]
    assert field == PRIMARY_FIELD


def test_a_preference_the_channel_cannot_meet_falls_back_instead_of_failing():
    """A channel with only Instagram Login must still work for a job that merely *prefers* Facebook."""
    channel = {"instagram_tokens": {"access_token": "IG", "provider": PROVIDER_INSTAGRAM}}
    field, tokens = select_slot(channel, PROVIDER_FACEBOOK)  # type: ignore[misc]
    assert field == PRIMARY_FIELD and tokens["access_token"] == "IG"
    assert not has_provider(channel, PROVIDER_FACEBOOK)


def test_a_channel_with_no_token_selects_nothing():
    assert select_slot({}) is None
    assert select_slot({PRIMARY_FIELD: {"provider": PROVIDER_FACEBOOK}}) is None  # no access_token


def test_a_token_stored_before_the_provider_field_existed_counts_as_facebook():
    channel = {PRIMARY_FIELD: {"access_token": "OLD"}}
    assert has_provider(channel, PROVIDER_FACEBOOK)


def test_a_facebook_token_is_addressed_by_its_own_business_id_not_the_channels_scoped_one():
    channel = _both()
    assert ig_user_id_for(channel, channel[ALT_FIELD]) == "business-id"
    assert ig_user_id_for(channel, channel[PRIMARY_FIELD]) == "ig-scoped-id"


# --- storing a token ----------------------------------------------------------


def test_the_first_token_becomes_the_primary():
    assert slot_for_store({}, PROVIDER_INSTAGRAM) == PRIMARY_FIELD


def test_a_token_of_the_same_login_type_replaces_it_rather_than_adding_a_third():
    assert slot_for_store(_both(), PROVIDER_INSTAGRAM) == PRIMARY_FIELD
    assert slot_for_store(_both(), PROVIDER_FACEBOOK) == ALT_FIELD


def test_a_token_of_the_other_type_goes_to_the_alt_slot_and_leaves_the_primary_alone():
    channel = {PRIMARY_FIELD: {"access_token": "IG", "provider": PROVIDER_INSTAGRAM}}
    assert slot_for_store(channel, PROVIDER_FACEBOOK) == ALT_FIELD


def test_a_token_doc_is_trimmed_and_carries_the_business_id_only_when_given():
    doc = build_token_doc("  abc\n", "bogus", None, "biz")
    assert doc["access_token"] == "abc" and doc["provider"] == PROVIDER_FACEBOOK and doc["instagram_user_id"] == "biz"
    assert "instagram_user_id" not in build_token_doc("abc", PROVIDER_INSTAGRAM)


# --- secrets never leave ------------------------------------------------------


def test_every_token_slot_is_stripped_from_channel_responses():
    """A future slot added to the doc but not to the exclusion list would leak a token."""
    projection = secret_projection()
    assert set(projection) == set(SECRET_CHANNEL_FIELDS)
    assert PRIMARY_FIELD in projection and ALT_FIELD in projection and "youtube_tokens" in projection
    assert all(v == 0 for v in projection.values())


def test_the_slot_summary_names_login_types_but_never_a_token():
    rows = summarise_slots(_both())
    assert [r["provider"] for r in rows] == [PROVIDER_INSTAGRAM, PROVIDER_FACEBOOK]
    assert "TOKEN" not in repr(rows)


# --- the service manager ------------------------------------------------------


class _Channels:
    def __init__(self, channel: dict[str, Any]) -> None:
        self.channel = channel
        self.writes: list[dict[str, Any]] = []

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return self.channel if query.get("channel_id") == self.channel["channel_id"] else None

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> None:
        self.writes.append(update["$set"])


class _Db:
    def __init__(self, channel: dict[str, Any]) -> None:
        self.channels = _Channels(channel)


def test_the_manager_hands_back_the_facebook_client_with_the_business_id():
    manager = InstagramServiceManager(_Db(_both()))
    service, user_id = asyncio.run(manager.get_service_and_user_id("tav", PROVIDER_FACEBOOK))  # type: ignore[misc]
    assert service._provider == PROVIDER_FACEBOOK and user_id == "business-id"
    service, user_id = asyncio.run(manager.get_service_and_user_id("tav", PROVIDER_INSTAGRAM))  # type: ignore[misc]
    assert service._provider == PROVIDER_INSTAGRAM and user_id == "ig-scoped-id"


def test_each_login_type_gets_its_own_cached_client_and_invalidate_clears_both():
    manager = InstagramServiceManager(_Db(_both()))
    fb = asyncio.run(manager.get_service("tav", PROVIDER_FACEBOOK))
    ig = asyncio.run(manager.get_service("tav", PROVIDER_INSTAGRAM))
    assert fb is not ig
    assert asyncio.run(manager.get_service("tav", PROVIDER_FACEBOOK)) is fb  # cached
    manager.invalidate("tav")
    assert asyncio.run(manager.get_service("tav", PROVIDER_FACEBOOK)) is not fb


def test_a_refreshed_alt_token_is_written_back_to_the_alt_slot_not_the_primary(monkeypatch: pytest.MonkeyPatch):
    class _Resp:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, Any]:
            return {"access_token": "NEW", "expires_in": 100}

    monkeypatch.setattr(instagram_module.requests, "get", lambda *a, **k: _Resp())
    db = _Db(_both())
    manager = InstagramServiceManager(db)
    fb = asyncio.run(manager.get_service("tav", PROVIDER_FACEBOOK))
    assert fb is not None
    assert asyncio.run(fb.refresh_token("app", "secret")) == "NEW"
    written = db.channels.writes[0]
    assert f"{ALT_FIELD}.access_token" in written and f"{PRIMARY_FIELD}.access_token" not in written


# --- features that choose a token --------------------------------------------


def test_an_instagram_login_channel_with_a_facebook_token_can_run_competitor_lookups():
    assert is_business_discovery_capable(_both())
    ig_only = {k: v for k, v in _both().items() if k != ALT_FIELD}
    assert not is_business_discovery_capable(ig_only)


def test_a_facebook_alt_token_without_a_business_id_cannot_address_a_lookup():
    channel = _both()
    channel[ALT_FIELD] = {"access_token": "FB", "provider": PROVIDER_FACEBOOK}
    channel.pop("instagram_user_id")
    assert not is_business_discovery_capable(channel)


class _Graph:
    """A canned Graph response: ``ok`` and ``json()`` are all the lookup reads."""

    def __init__(self, body: dict[str, Any], status: int = 200) -> None:
        self._body, self.status_code = body, status
        self.ok = status < 400

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if not self.ok:
            raise instagram_module.requests.HTTPError(response=self)  # type: ignore[arg-type]


_PAGE_TOKEN_ERROR = {"error": {"message": "(#100) Tried accessing nonexisting field (accounts)", "code": 100}}


def _route(monkeypatch: pytest.MonkeyPatch, routes: dict[str, _Graph]) -> list[str]:
    """Answer Graph calls by the endpoint's last path segment; returns the endpoints called."""
    called: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> _Graph:
        endpoint = url.rsplit("/", 1)[-1]
        called.append(endpoint)
        return routes[endpoint]

    monkeypatch.setattr(instagram_module.requests, "get", fake_get)
    return called


def test_the_business_id_is_found_by_username_across_the_pages_a_user_token_reaches(monkeypatch: pytest.MonkeyPatch):
    _route(
        monkeypatch,
        {
            "accounts": _Graph(
                {
                    "data": [
                        {"id": "p1", "instagram_business_account": {"id": "111", "username": "someone_else"}},
                        {"id": "p2", "instagram_business_account": {"id": "222", "username": "TryAlgoViz"}},
                        {"id": "p3"},
                    ]
                }
            )
        },
    )
    assert find_business_account_id("tok", {"@tryalgoviz"}) == "222"
    assert find_business_account_id("tok", {"nobody"}) is None


def test_a_page_token_is_read_directly_because_it_has_no_accounts_edge(monkeypatch: pytest.MonkeyPatch):
    """The Graph API Explorer can issue a Page token; /me is then the Page, and /me/accounts does not exist."""
    called = _route(
        monkeypatch,
        {
            "accounts": _Graph(_PAGE_TOKEN_ERROR, status=400),
            "me": _Graph(
                {"id": "page", "instagram_business_account": {"id": "17841418776716682", "username": "tryalgoviz"}}
            ),
        },
    )
    assert find_business_account_id("page-token", {"tryalgoviz"}) == "17841418776716682"
    assert called == ["accounts", "me"]


def test_a_page_token_for_a_different_account_is_refused_not_attached(monkeypatch: pytest.MonkeyPatch):
    _route(
        monkeypatch,
        {
            "accounts": _Graph(_PAGE_TOKEN_ERROR, status=400),
            "me": _Graph({"id": "page", "instagram_business_account": {"id": "999", "username": "other_account"}}),
        },
    )
    assert find_business_account_id("page-token", {"tryalgoviz"}) is None


def test_a_page_with_no_linked_instagram_account_matches_nothing(monkeypatch: pytest.MonkeyPatch):
    _route(monkeypatch, {"accounts": _Graph(_PAGE_TOKEN_ERROR, status=400), "me": _Graph({"id": "page"})})
    assert find_business_account_id("page-token", {"tryalgoviz"}) is None


def test_a_real_failure_is_not_mistaken_for_a_page_token(monkeypatch: pytest.MonkeyPatch):
    """An expired token must surface as an error, not be quietly retried against /me."""
    expired = {"error": {"message": "Error validating access token: Session has expired", "code": 190}}
    called = _route(monkeypatch, {"accounts": _Graph(expired, status=400)})
    with pytest.raises(instagram_module.requests.HTTPError):
        find_business_account_id("old", {"tryalgoviz"})
    assert called == ["accounts"]
