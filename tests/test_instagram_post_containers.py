"""The Graph requests behind image / carousel / story posts.

Nothing here reaches Instagram: ``_post`` / ``_get`` are replaced, and the
tests pin the exact parameters we send, since a wrong one only shows up as a
container ERROR at publish time.
"""

from __future__ import annotations

import pytest
import requests

from app.services.instagram import PROVIDER_INSTAGRAM, InstagramService


def _svc(provider="facebook"):
    svc = InstagramService(access_token="t", provider=provider)
    calls = []

    def fake_post(endpoint, params=None):
        calls.append((endpoint, dict(params or {})))
        return {"id": f"c{len(calls)}"}

    svc._post = fake_post  # type: ignore[method-assign]
    return svc, calls


def test_single_image_container_carries_caption_and_alt_text_but_no_media_type():
    svc, calls = _svc()
    assert svc.create_image_container("ig1", "https://img", caption="hi", alt_text="a cat") == "c1"
    assert calls == [("ig1/media", {"image_url": "https://img", "caption": "hi", "alt_text": "a cat"})]


def test_carousel_image_item_has_no_caption():
    svc, calls = _svc()
    svc.create_image_container("ig1", "https://img", caption="ignored", is_carousel_item=True)
    assert calls[0][1] == {"image_url": "https://img", "is_carousel_item": "true"}


def test_carousel_video_item_and_parent():
    svc, calls = _svc()
    svc.create_carousel_video_item("ig1", "https://vid")
    svc.create_carousel_container("ig1", ["a", "b"], "cap")
    assert calls[0][1] == {"media_type": "VIDEO", "is_carousel_item": "true", "video_url": "https://vid"}
    assert calls[1][1] == {"media_type": "CAROUSEL", "children": "a,b", "caption": "cap"}


def test_story_container_and_instagram_login_uses_me():
    svc, calls = _svc(PROVIDER_INSTAGRAM)
    svc.create_story_container("ig1", video_url="https://vid")
    assert calls == [("me/media", {"media_type": "STORIES", "video_url": "https://vid"})]
    with pytest.raises(ValueError):
        svc.create_story_container("ig1")


def test_publishing_limit_is_flattened():
    svc, _ = _svc()
    svc._get = lambda endpoint, params=None: {  # type: ignore[method-assign]
        "data": [{"config": {"quota_total": 100, "quota_duration": 86400}, "quota_usage": 1}]
    }
    assert svc.get_publishing_limit("ig1") == {"quota_total": 100, "quota_usage": 1, "quota_duration": 86400}


def test_media_page_cursor_only_when_there_is_a_next_page():
    svc, _ = _svc()
    bodies = [
        {"data": [{"id": "1"}], "paging": {"cursors": {"after": "AA"}, "next": "https://next"}},
        {"data": [{"id": "2"}], "paging": {"cursors": {"after": "BB"}}},
    ]
    seen = []

    def fake_get(endpoint, params=None):
        seen.append((endpoint, params))
        return bodies.pop(0)

    svc._get = fake_get  # type: ignore[method-assign]
    assert svc.get_media_page("ig1", limit=24)["next_cursor"] == "AA"
    assert svc.get_media_page("ig1", after="AA")["next_cursor"] is None
    assert "media_product_type" in seen[0][1]["fields"]
    assert "children{media_type,media_url,thumbnail_url}" in seen[0][1]["fields"]
    assert seen[1][1]["after"] == "AA"


def test_insights_fall_back_to_per_metric_when_the_batch_is_refused():
    svc, _ = _svc()

    def fake_get(endpoint, params=None):
        metric = params["metric"]
        if "," in metric or metric == "views":
            raise requests.HTTPError("unsupported metric")
        return {"data": [{"name": metric, "values": [{"value": 7}]}]}

    svc._get = fake_get  # type: ignore[method-assign]
    values, unavailable = svc.get_media_insights("m1", ["views", "reach"])
    assert values == {"reach": 7}
    assert unavailable == ["views"]
