"""The admin competitor feed: its auth gate, source-channel choice, and the
honesty of what it reports.

The feed serves a different application, so the things worth guarding are the
ones a consumer cannot see for themselves — that a hidden like count is not
reported as zero, that a stale cursor does not read as "nothing new", and that
a truncated history is labelled as truncated.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status

from app.routers import competitor_feed as feed_router
from app.services.competitor_feed import (
    is_business_discovery_capable,
    normalise_post,
    select_source_channel,
    summarise,
    trim_to_new,
)

# --- the auth gate ------------------------------------------------------------


def _set_key(monkeypatch, value: str | None) -> None:
    monkeypatch.setattr(feed_router, "get_settings", lambda: SimpleNamespace(COMPETITOR_FEED_KEY=value))


def _verify(key: str | None):
    return asyncio.run(feed_router.verify_feed_key(key))


class TestFeedKeyGate:
    def test_unconfigured_disables_the_feed(self, monkeypatch):
        """Fail closed: no secret on the server means off for everyone, not open
        to everyone."""
        _set_key(monkeypatch, None)
        with pytest.raises(HTTPException) as exc:
            _verify("anything")
        assert exc.value.status_code == status.HTTP_403_FORBIDDEN

    def test_empty_config_disables_the_feed(self, monkeypatch):
        _set_key(monkeypatch, "")
        with pytest.raises(HTTPException) as exc:
            _verify("")
        assert exc.value.status_code == status.HTTP_403_FORBIDDEN

    def test_a_wrong_key_is_rejected(self, monkeypatch):
        _set_key(monkeypatch, "right")
        with pytest.raises(HTTPException) as exc:
            _verify("wrong")
        assert exc.value.status_code == status.HTTP_403_FORBIDDEN

    def test_a_missing_key_is_rejected(self, monkeypatch):
        _set_key(monkeypatch, "right")
        with pytest.raises(HTTPException) as exc:
            _verify(None)
        assert exc.value.status_code == status.HTTP_403_FORBIDDEN

    def test_the_right_key_passes(self, monkeypatch):
        _set_key(monkeypatch, "right")
        assert _verify("right") == "right"

    def test_rejection_is_403_not_401(self, monkeypatch):
        """401 reads as an API-key failure and logs the dashboard out; this is a
        separate pre-shared secret and must not be confused with one."""
        _set_key(monkeypatch, "right")
        with pytest.raises(HTTPException) as exc:
            _verify("wrong")
        assert exc.value.status_code != status.HTTP_401_UNAUTHORIZED


# --- choosing whose token runs the query --------------------------------------


def _channel(channel_id: str, provider: str | None = None, **overrides) -> dict:
    tokens: dict = {"access_token": "t"}
    if provider is not None:
        tokens["provider"] = provider
    doc = {
        "channel_id": channel_id,
        "platform": "instagram",
        "instagram_user_id": "ig1",
        "instagram_tokens": tokens,
    }
    doc.update(overrides)
    return doc


def test_a_facebook_login_channel_is_capable():
    assert is_business_discovery_capable(_channel("a", "facebook"))


def test_an_instagram_login_channel_is_not():
    """business_discovery does not exist on graph.instagram.com."""
    assert not is_business_discovery_capable(_channel("a", "instagram"))


def test_a_channel_predating_the_provider_field_counts_as_facebook():
    """Channels registered before the field existed are Facebook-Login, which is
    what the service defaults to when reading them."""
    assert is_business_discovery_capable(_channel("a"))


def test_a_channel_without_our_own_ig_id_is_not_capable():
    """The query is addressed to our own node, so there is nothing to send it to."""
    assert not is_business_discovery_capable(_channel("a", "facebook", instagram_user_id=None))


def test_a_channel_with_no_token_is_not_capable():
    assert not is_business_discovery_capable({"platform": "instagram", "instagram_user_id": "ig1"})


def test_the_first_capable_channel_is_chosen():
    chosen = select_source_channel([_channel("ig-login", "instagram"), _channel("fb", "facebook")])
    assert chosen is not None and chosen["channel_id"] == "fb"


def test_a_named_channel_is_honoured():
    chosen = select_source_channel([_channel("a", "facebook"), _channel("b", "facebook")], "b")
    assert chosen is not None and chosen["channel_id"] == "b"


def test_a_named_channel_that_cannot_run_the_query_is_refused_not_swapped():
    """Silently using a different account than the caller asked for would make a
    per-channel rate limit impossible to reason about."""
    assert select_source_channel([_channel("a", "facebook"), _channel("b", "instagram")], "b") is None


def test_no_capable_channel_returns_none():
    assert select_source_channel([_channel("a", "instagram")]) is None


# --- shaping a post -----------------------------------------------------------


def test_a_hidden_like_count_is_none_not_zero():
    """Meta omits the field when an account hides likes. Reporting 0 would state
    a fact we do not have — and this really happens on live accounts."""
    assert normalise_post({"id": "1", "comments_count": 5})["like_count"] is None


def test_a_real_zero_is_kept_as_zero():
    assert normalise_post({"id": "1", "like_count": 0})["like_count"] == 0


def test_carousel_frames_are_flattened():
    post = normalise_post(
        {
            "id": "1",
            "media_type": "CAROUSEL_ALBUM",
            "media_url": "https://cdn/cover.jpg",
            "children": {"data": [{"id": "c1", "media_type": "IMAGE", "media_url": "https://cdn/1.jpg"}]},
        }
    )
    assert post["media_url"] == "https://cdn/cover.jpg"
    assert post["children"] == [
        {"id": "c1", "media_type": "IMAGE", "media_url": "https://cdn/1.jpg", "thumbnail_url": None}
    ]


def test_a_video_keeps_its_poster_and_has_no_media_url():
    """Instagram gives no downloadable URL for someone else's video — only a
    thumbnail and the permalink."""
    post = normalise_post(
        {"id": "1", "media_type": "VIDEO", "thumbnail_url": "https://cdn/poster.jpg", "permalink": "https://ig/p/1"}
    )
    assert post["media_url"] is None
    assert post["thumbnail_url"] == "https://cdn/poster.jpg"
    assert post["permalink"] == "https://ig/p/1"


def test_counts_are_grouped_by_media_type():
    posts = [{"media_type": "CAROUSEL_ALBUM"}, {"media_type": "CAROUSEL_ALBUM"}, {"media_type": "VIDEO"}]
    assert summarise(posts) == {"CAROUSEL_ALBUM": 2, "VIDEO": 1}


# --- incremental fetching -----------------------------------------------------


def _posts(*ids_and_times):
    return [{"id": i, "timestamp": t} for i, t in ids_and_times]


def test_without_a_marker_everything_is_returned():
    posts = _posts(("a", "2026-09-03"), ("b", "2026-09-02"))
    trimmed, found = trim_to_new(posts)
    assert len(trimmed) == 2
    assert found is None


def test_only_posts_newer_than_the_marker_come_back():
    """Media is newest-first, so everything before the marker is new."""
    posts = _posts(("c", "2026-09-03"), ("b", "2026-09-02"), ("a", "2026-09-01"))
    trimmed, found = trim_to_new(posts, since_id="b")
    assert [p["id"] for p in trimmed] == ["c"]
    assert found is True


def test_a_marker_on_the_newest_post_yields_nothing_new():
    posts = _posts(("c", "2026-09-03"), ("b", "2026-09-02"))
    trimmed, found = trim_to_new(posts, since_id="c")
    assert trimmed == []
    assert found is True


def test_an_unfindable_marker_returns_everything_and_says_so():
    """The marker is older than Meta's window, or its post was deleted. Returning
    nothing would read as "no new posts" when the truth is "we cannot tell" — so
    the caller gets the window plus a flag to de-duplicate against."""
    posts = _posts(("c", "2026-09-03"), ("b", "2026-09-02"))
    trimmed, found = trim_to_new(posts, since_id="long-gone")
    assert len(trimmed) == 2
    assert found is False


def test_a_timestamp_marker_is_an_alternative_to_an_id():
    """Survives the marker post being deleted, which an id cannot."""
    posts = _posts(("c", "2026-09-03"), ("b", "2026-09-02"), ("a", "2026-09-01"))
    trimmed, _ = trim_to_new(posts, since_timestamp="2026-09-02")
    assert [p["id"] for p in trimmed] == ["c"]


def test_an_id_marker_wins_when_both_are_sent():
    posts = _posts(("c", "2026-09-03"), ("b", "2026-09-02"), ("a", "2026-09-01"))
    trimmed, found = trim_to_new(posts, since_id="b", since_timestamp="2020-01-01")
    assert [p["id"] for p in trimmed] == ["c"]
    assert found is True
