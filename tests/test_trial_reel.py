"""Trial reels: validation, the container parameter, and carrying the choice to publish."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest

from app.services.instagram import InstagramService
from app.services.trial_reel import MANUAL, SS_PERFORMANCE, trial_params_json, validate_strategy
from app.services.video_service import VideoService
from app.timezone import now_ist

# --- the rules ----------------------------------------------------------------


def test_a_known_strategy_is_accepted_in_any_case():
    assert validate_strategy(" manual ", "instagram") == MANUAL
    assert validate_strategy("ss_performance", "instagram") == SS_PERFORMANCE


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_means_not_a_trial_reel(blank):
    assert validate_strategy(blank, "instagram") is None


def test_an_unknown_strategy_is_refused():
    with pytest.raises(ValueError, match="MANUAL"):
        validate_strategy("AUTO", "instagram")


def test_youtube_has_no_trial_reels():
    with pytest.raises(ValueError, match="Instagram"):
        validate_strategy(MANUAL, "youtube")


def test_blank_is_fine_on_youtube():
    """Clearing a setting that was never there must not error."""
    assert validate_strategy("", "youtube") is None


def test_params_are_a_json_object_as_meta_expects():
    assert json.loads(trial_params_json(MANUAL) or "") == {"graduation_strategy": "MANUAL"}
    assert trial_params_json(None) is None


# --- the container request ----------------------------------------------------


def _publish(**kwargs: Any) -> dict[str, Any]:
    svc = InstagramService(access_token="t")
    sent: dict[str, Any] = {}

    def fake_post(_endpoint, params=None):
        sent.update(params or {})
        return {"id": "c1"}

    svc._post = fake_post  # type: ignore[method-assign]
    svc.get_container_status = lambda _cid: ("FINISHED", "")  # type: ignore[method-assign]
    svc.publish_container = lambda _uid, _cid: "m1"  # type: ignore[method-assign]
    svc.publish_reel_from_url("123", "https://r2/v.mp4", "cap", **kwargs)
    return sent


def test_a_trial_reel_sends_trial_params():
    sent = _publish(trial_graduation=SS_PERFORMANCE)
    assert json.loads(sent["trial_params"]) == {"graduation_strategy": "SS_PERFORMANCE"}


def test_a_normal_reel_sends_no_trial_params():
    assert "trial_params" not in _publish()


# --- scheduling ---------------------------------------------------------------


class _Videos:
    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc
        self.sets: list[dict[str, Any]] = []

    async def find_one(self, *_a, **_k):
        return dict(self.doc)

    async def update_one(self, _flt, update, **_kw):
        self.sets.append(update.get("$set", {}))


class _Coll:
    async def find_one(self, *_a, **_k):
        return {"channel_id": "c1", "platform": "instagram"}

    async def update_one(self, *_a, **_k):
        return None


class _DB:
    def __init__(self, doc: dict[str, Any], platform: str = "instagram") -> None:
        self.videos = _Videos(doc)
        self.channels = _Coll()
        self.channels.find_one = self._channel(platform)  # type: ignore[method-assign]
        self.schedule_queue = _Coll()

    @staticmethod
    def _channel(platform: str):
        async def find_one(*_a, **_k):
            return {"channel_id": "c1", "platform": platform}

        return find_one


def test_rescheduling_can_set_a_trial_reel():
    db = _DB({"_id": 1, "channel_id": "c1", "video_id": "v1", "status": "queued"})
    asyncio.run(VideoService(db=db).reschedule_video("c1", "v1", now_ist() + timedelta(hours=1), None, "manual"))
    assert db.videos.sets[0]["trial_graduation"] == MANUAL


def test_rescheduling_without_mentioning_it_leaves_the_choice_alone():
    db = _DB({"_id": 1, "channel_id": "c1", "video_id": "v1", "status": "queued", "trial_graduation": MANUAL})
    asyncio.run(VideoService(db=db).reschedule_video("c1", "v1", now_ist() + timedelta(hours=1)))
    assert "trial_graduation" not in db.videos.sets[0]


def test_rescheduling_with_a_blank_turns_it_off():
    db = _DB({"_id": 1, "channel_id": "c1", "video_id": "v1", "status": "queued", "trial_graduation": MANUAL})
    asyncio.run(VideoService(db=db).reschedule_video("c1", "v1", now_ist() + timedelta(hours=1), None, ""))
    assert db.videos.sets[0]["trial_graduation"] is None


# --- an account that cannot make trial reels ----------------------------------


class _Refused:
    status_code = 400

    def json(self):
        return {"error": {"code": 10, "error_subcode": 2207081}}


def test_an_account_with_too_few_followers_gets_a_clear_failure_not_a_normal_reel():
    import requests

    from app.services.trial_reel import TrialReelNotAvailableError

    svc = InstagramService(access_token="t")

    def refuse(_endpoint, params=None):
        raise requests.HTTPError("400", response=_Refused())  # type: ignore[arg-type]

    svc._post = refuse  # type: ignore[method-assign]
    with pytest.raises(TrialReelNotAvailableError, match="enough followers"):
        svc.publish_reel_from_url("123", "https://r2/v.mp4", "cap", trial_graduation=MANUAL)


def test_rescheduling_as_a_normal_reel_removes_the_graduate_check_too():
    """Changing your mind before it publishes: no trial, and no timers left behind."""
    check = {"after_hours": [24, 48], "match": "all", "conditions": [{"metric": "views", "minimum": 100}]}
    db = _DB(
        {
            "_id": 1,
            "channel_id": "c1",
            "video_id": "v1",
            "status": "queued",
            "trial_graduation": MANUAL,
            "trial_check": check,
        }
    )
    asyncio.run(VideoService(db=db).reschedule_video("c1", "v1", now_ist() + timedelta(hours=1), None, ""))
    assert db.videos.sets[0]["trial_graduation"] is None
    assert db.videos.sets[0]["trial_check"] is None
