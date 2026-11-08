"""Collaborators on a reel or feed post: cleaning, what a request writes, and what goes to Instagram."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import requests

from app.services import auto_publisher
from app.services.instagram import InstagramService
from app.services.reel_collaborators import (
    api_collaborators,
    clean_collaborators,
    collaborator_fields,
    is_collaborator_refusal,
)
from tests.test_instagram_cover_url import _FakeDB, _FakeInstagram, _FakeR2


def test_usernames_are_cleaned_and_kept_once() -> None:
    assert clean_collaborators([" @MehrKaul ", "mehrkaul", "zara.kapoor_1", ""]) == ["mehrkaul", "zara.kapoor_1"]
    assert clean_collaborators([]) == []
    assert clean_collaborators(None) is None


def test_a_bad_username_or_too_many_is_refused() -> None:
    with pytest.raises(ValueError, match="not an Instagram username"):
        clean_collaborators(["mehr kaul"])
    with pytest.raises(ValueError, match="at most 3"):
        clean_collaborators(["a", "b", "c", "d"])


def test_a_request_that_says_nothing_writes_nothing_and_an_empty_list_clears() -> None:
    assert collaborator_fields(None, "instagram") == {}
    assert collaborator_fields([], "instagram") == {"collaborators": []}
    assert collaborator_fields(["@mehrkaul"], "instagram") == {"collaborators": ["mehrkaul"]}


def test_only_instagram_takes_collaborators_and_never_its_own_account() -> None:
    with pytest.raises(ValueError, match="Instagram"):
        collaborator_fields(["mehrkaul"], "youtube")
    assert collaborator_fields([], "youtube") == {"collaborators": []}
    with pytest.raises(ValueError, match="own account"):
        collaborator_fields(["ZaraKapoor"], "instagram", own_username="zarakapoor")


def test_only_a_non_empty_list_is_sent() -> None:
    assert api_collaborators({"collaborators": ["mehrkaul"]}) == ["mehrkaul"]
    assert api_collaborators({"collaborators": []}) is None
    assert api_collaborators({}) is None


def test_only_a_collaborator_complaint_counts_as_a_collaborator_refusal() -> None:
    assert is_collaborator_refusal("The user mehrkaul cannot be tagged as a collaborator")
    assert not is_collaborator_refusal("Param location_id is not a valid location page ID")


# --- the container request ---------------------------------------------------


def _sent(call: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    svc = InstagramService(access_token="t")
    seen: dict[str, Any] = {}

    def fake_post(_endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        seen.update(params or {})
        return {"id": "c1", "uri": "https://upload"}

    svc._post = fake_post  # type: ignore[method-assign]
    svc.get_container_status = lambda _cid: ("FINISHED", "")  # type: ignore[method-assign]
    svc.publish_container = lambda _uid, _cid: "m1"  # type: ignore[method-assign]
    getattr(svc, call)(*args, **kwargs)
    return seen


def test_the_reel_container_carries_collaborators_as_a_json_list_only_when_set() -> None:
    sent = _sent("publish_reel_from_url", "123", "https://r2/v.mp4", "cap", collaborators=["mehrkaul"])
    assert json.loads(sent["collaborators"]) == ["mehrkaul"]
    assert "collaborators" not in _sent("publish_reel_from_url", "123", "https://r2/v.mp4", "cap")
    assert "collaborators" not in _sent("publish_reel_from_url", "123", "https://r2/v.mp4", "cap", collaborators=[])
    resumable = _sent("create_reel_container", "123", "cap", collaborators=["mehrkaul"])
    assert json.loads(resumable["collaborators"]) == ["mehrkaul"]


def test_a_carousel_carries_them_on_its_parent_and_never_on_a_slide() -> None:
    parent = _sent("create_carousel_container", "123", ["a", "b"], "cap", collaborators=["mehrkaul"])
    assert json.loads(parent["collaborators"]) == ["mehrkaul"]
    slide = _sent("create_image_container", "123", "https://r2/i.jpg", is_carousel_item=True, collaborators=["x"])
    assert "collaborators" not in slide
    single = _sent("create_image_container", "123", "https://r2/i.jpg", caption="c", collaborators=["mehrkaul"])
    assert json.loads(single["collaborators"]) == ["mehrkaul"]


# --- the auto-publisher --------------------------------------------------------


def _refusal(message: str) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = 400
    response._content = json.dumps({"error": {"message": message}}).encode()
    return requests.HTTPError(response=response)


def _run(ig: _FakeInstagram, video: dict[str, Any]) -> tuple[bool, _FakeDB]:
    db = _FakeDB()
    ok = asyncio.run(
        auto_publisher._publish_one_reel(
            db=db,
            r2_service=_FakeR2(),
            instagram_service=ig,
            channel_doc={"channel_id": "c1", "instagram_user_id": "ig1"},
            video_doc={"video_id": "v1", "r2_object_key": "c1/v1.mp4", "title": "t", **video},
            queue_entry={"_id": "q1"},
        )
    )
    return ok, db


def test_a_reel_without_collaborators_sends_none() -> None:
    ig = _FakeInstagram()
    ok, _ = _run(ig, {})
    assert ok and ig.kwargs["collaborators"] is None


def test_a_reels_collaborators_are_sent() -> None:
    ig = _FakeInstagram()
    ok, _ = _run(ig, {"collaborators": ["mehrkaul"], "trial_graduation": "MANUAL"})
    assert ok and ig.kwargs["collaborators"] == ["mehrkaul"] and ig.kwargs["trial_graduation"] == "MANUAL"


class _Refusing(_FakeInstagram):
    def __init__(self, refuse: dict[str, str]) -> None:
        super().__init__()
        self.refuse = refuse
        self.calls: list[tuple[Any, Any]] = []

    def publish_reel_from_url(self, **kwargs: Any) -> str:
        self.calls.append((kwargs["location_id"], kwargs["collaborators"]))
        for key, message in self.refuse.items():
            if kwargs[key]:
                raise _refusal(message)
        return "media-1"


def _set_fields(db: _FakeDB) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for _flt, update in db.videos.updates:
        fields.update(update.get("$set", {}))
    return fields


def test_refused_collaborators_publish_without_them_and_keep_the_reason() -> None:
    ig = _Refusing({"collaborators": "The user can't be invited as a collaborator"})
    ok, db = _run(ig, {"collaborators": ["mehrkaul"], "location_id": "999"})
    assert ok and ig.calls == [("999", ["mehrkaul"]), ("999", None)]
    assert "collaborator" in _set_fields(db)["collaborators_dropped_reason"]


def test_both_refused_drops_each_once_then_publishes() -> None:
    ig = _Refusing({"collaborators": "collaborator not allowed", "location_id": "not a valid location"})
    ok, db = _run(ig, {"collaborators": ["mehrkaul"], "location_id": "999"})
    assert ok and ig.calls == [("999", ["mehrkaul"]), ("999", None), (None, None)]
    fields = _set_fields(db)
    assert "collaborators_dropped_reason" in fields and "location_dropped_reason" in fields


def test_any_other_refusal_still_fails_the_publish() -> None:
    ig = _Refusing({"collaborators": "Too many hashtags used"})
    ok, _ = _run(ig, {"collaborators": ["mehrkaul"]})
    assert not ok and len(ig.calls) == 1
