"""Graduate checks for manual trial reels: the bar, the timer, and the email."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from app.services.trial_graduation import (
    FAILED,
    MET,
    MISSED,
    PENDING,
    TrialCheck,
    arm_fields,
    evaluate,
    trial_fields,
)
from app.services.trial_graduation_cron import MAX_FETCH_ATTEMPTS, Snapshot, run_trial_checks
from app.services.trial_graduation_email import format_graduate_email

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _check(match: str = "all", **mins: float) -> dict[str, Any]:
    return {
        "after_hours": 24,
        "match": match,
        "conditions": [{"metric": m, "minimum": v} for m, v in (mins or {"views": 1000}).items()],
    }


# --- the request --------------------------------------------------------------


def test_a_check_needs_at_least_one_condition():
    with pytest.raises(ValidationError):
        TrialCheck(after_hours=24, conditions=[])


@pytest.mark.parametrize("hours", [0, 0.1, 169])
def test_hours_outside_the_sensible_range_are_refused(hours):
    with pytest.raises(ValidationError):
        TrialCheck(after_hours=hours, conditions=[{"metric": "views", "minimum": 1}])


def test_an_unknown_metric_is_refused():
    with pytest.raises(ValidationError):
        TrialCheck(after_hours=24, conditions=[{"metric": "clicks", "minimum": 1}])


def test_a_check_is_stored_alongside_a_manual_strategy():
    check = TrialCheck(**_check(views=500))
    fields = trial_fields("MANUAL", check, None, "instagram")
    assert fields["trial_graduation"] == "MANUAL"
    assert fields["trial_check"]["conditions"] == [{"metric": "views", "minimum": 500.0}]


def test_a_check_with_an_automatic_trial_is_refused():
    with pytest.raises(ValueError, match="share yourself"):
        trial_fields("SS_PERFORMANCE", TrialCheck(**_check()), None, "instagram")


def test_a_check_alone_works_when_the_reel_is_already_manual():
    assert "trial_check" in trial_fields(None, TrialCheck(**_check()), "MANUAL", "instagram")


def test_a_check_alone_is_refused_when_the_reel_is_not_a_trial():
    with pytest.raises(ValueError):
        trial_fields(None, TrialCheck(**_check()), None, "instagram")


def test_turning_the_trial_off_clears_the_check():
    assert trial_fields("", None, "MANUAL", "instagram") == {"trial_graduation": None, "trial_check": None}


def test_saying_nothing_changes_nothing():
    assert trial_fields(None, None, "MANUAL", "instagram") == {}


# --- the timer ----------------------------------------------------------------


def test_the_timer_starts_at_publish():
    video = {"trial_graduation": "MANUAL", "trial_check": _check()}
    fields = arm_fields(video, NOW)
    assert fields == {"trial_check_state": PENDING, "trial_check_due_at": NOW + timedelta(hours=24)}


def test_no_timer_without_a_check_or_a_manual_trial():
    assert arm_fields({"trial_graduation": "MANUAL"}, NOW) == {}
    assert arm_fields({"trial_graduation": "SS_PERFORMANCE", "trial_check": _check()}, NOW) == {}


# --- the bar ------------------------------------------------------------------


def test_all_requires_every_condition():
    check = _check("all", views=1000, saves=50)
    assert evaluate(check, {"views": 2000, "saves": 60}).met
    assert not evaluate(check, {"views": 2000, "saves": 10}).met


def test_any_requires_one_condition():
    check = _check("any", views=1000, saves=50)
    assert evaluate(check, {"views": 10, "saves": 60}).met
    assert not evaluate(check, {"views": 10, "saves": 10}).met


def test_the_minimum_itself_counts():
    assert evaluate(_check(views=1000), {"views": 1000}).met


def test_a_metric_instagram_did_not_report_cannot_pass():
    outcome = evaluate(_check(saves=1), {"views": 5000})
    assert not outcome.met
    assert outcome.results[0].actual is None


# --- the email ----------------------------------------------------------------


def test_the_email_carries_the_link_and_the_numbers():
    outcome = evaluate(_check(views=1000), {"views": 2500})
    mail = format_graduate_email(
        title="Sorting <fast>",
        channel="TryAlgoViz",
        permalink="https://www.instagram.com/reel/abc/",
        after_hours=24,
        match="all",
        results=outcome.results,
    )
    assert "https://www.instagram.com/reel/abc/" in mail.text
    assert "Views: 2500 (needed 1000)" in mail.text
    assert 'href="https://www.instagram.com/reel/abc/"' in mail.html
    assert "&lt;fast&gt;" in mail.html and "<fast>" not in mail.html


# --- the cron pass ------------------------------------------------------------


class _Videos:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs

    def find(self, query):
        def matches(d: dict[str, Any]) -> bool:
            for k, cond in query.items():
                v = d.get(k)
                if isinstance(cond, dict):
                    if "$lte" in cond and not (v is not None and v <= cond["$lte"]):
                        return False
                    if "$ne" in cond and v == cond["$ne"]:
                        return False
                    if "$not" in cond and v is not None and v >= cond["$not"]["$gte"]:
                        return False
                elif v != cond:
                    return False
            return True

        found = [d for d in self.docs if matches(d)]

        class _Cursor:
            async def to_list(self, length=None):
                return found

        return _Cursor()

    async def update_one(self, flt, update, **_k):
        for d in self.docs:
            if d["_id"] == flt["_id"]:
                d.update(update.get("$set", {}))


class _DB:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.videos = _Videos(docs)


def _video(**over: Any) -> dict[str, Any]:
    doc = {
        "_id": 1,
        "channel_id": "TryAlgoViz",
        "video_id": "v1",
        "title": "Sorting",
        "instagram_media_id": "m1",
        "trial_graduation": "MANUAL",
        "trial_check": _check(views=1000),
        "trial_check_state": PENDING,
        "trial_check_due_at": NOW - timedelta(minutes=1),
    }
    doc.update(over)
    return doc


def _run(docs, snapshot=None, error=None):
    sent: list[str] = []

    async def fetch(_c, _m):
        if error:
            raise error
        return snapshot or Snapshot("https://ig/reel/1", {"views": 5000})

    async def send(subject, _t, _h):
        sent.append(subject)
        return True

    stats = asyncio.run(run_trial_checks(_DB(docs), fetch, send, now=NOW))
    return stats, sent


def test_a_due_reel_over_the_bar_is_emailed_once():
    docs = [_video()]
    _, sent = _run(docs)
    assert docs[0]["trial_check_state"] == MET and docs[0]["trial_check_emailed"] is True
    assert len(sent) == 1
    _, again = _run(docs)
    assert again == []


def test_a_reel_under_the_bar_sends_nothing():
    docs = [_video()]
    _, sent = _run(docs, Snapshot("https://ig/reel/1", {"views": 5}))
    assert docs[0]["trial_check_state"] == MISSED and sent == []


def test_a_reel_that_is_not_due_yet_is_left_alone():
    docs = [_video(trial_check_due_at=NOW + timedelta(hours=2))]
    stats, sent = _run(docs)
    assert stats["checked"] == 0 and docs[0]["trial_check_state"] == PENDING and sent == []


def test_a_restart_picks_up_a_check_that_came_due_while_the_server_was_down():
    """State lives on the video, so a due check from hours ago is found like any other."""
    docs = [_video(trial_check_due_at=NOW - timedelta(hours=9))]
    _, sent = _run(docs)
    assert len(sent) == 1


def test_a_failed_read_is_retried_not_dropped():
    docs = [_video()]
    _run(docs, error=RuntimeError("boom"))
    assert docs[0]["trial_check_state"] == PENDING and docs[0]["trial_check_attempts"] == 1


def test_a_read_that_never_works_eventually_gives_up():
    docs = [_video(trial_check_attempts=MAX_FETCH_ATTEMPTS - 1)]
    _run(docs, error=RuntimeError("boom"))
    assert docs[0]["trial_check_state"] == FAILED


def test_an_unsent_email_is_retried_on_a_later_pass():
    docs = [_video(trial_check_state=MET, trial_check_result={"met": True, "results": []}, trial_check_due_at=None)]
    _, sent = _run(docs)
    assert len(sent) == 1 and docs[0]["trial_check_emailed"] is True
