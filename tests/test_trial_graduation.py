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
    edit_running_check,
    evaluate,
    trial_fields,
    upgrade_check,
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
    assert fields["trial_check"]["groups"] == [{"conditions": [{"metric": "views", "op": "gte", "value": 500.0}]}]


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
    assert fields == {
        "trial_check_state": PENDING,
        "trial_check_stage": 0,
        "trial_check_due_at": NOW + timedelta(hours=24),
    }


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
    assert outcome.groups[0].results[0].actual is None


# --- the email ----------------------------------------------------------------


def test_the_email_carries_the_link_and_the_numbers():
    outcome = evaluate(_check(views=1000), {"views": 2500})
    mail = format_graduate_email(
        title="Sorting <fast>",
        channel="TryAlgoViz",
        permalink="https://www.instagram.com/reel/abc/",
        after_hours=24,
        groups=outcome.groups,
    )
    assert "https://www.instagram.com/reel/abc/" in mail.text
    assert "Views: 2,500 (needed at least 1,000)" in mail.text
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


def test_a_reel_under_the_bar_on_its_only_timer_has_missed_and_sends_nothing():
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


# --- several timers -----------------------------------------------------------

PUBLISHED = NOW - timedelta(hours=25)
LOW = Snapshot("https://ig/reel/1", {"views": 5})


def _two_timers(**over: Any) -> dict[str, Any]:
    check = {**_check(views=1000), "after_hours": [24, 48]}
    return _video(trial_check=check, published_at=PUBLISHED, **over)


def test_up_to_three_timers_are_allowed_and_a_fourth_is_not():
    cond = [{"metric": "views", "minimum": 1}]
    assert TrialCheck(after_hours=[6, 24, 48], conditions=cond).after_hours == [6, 24, 48]
    with pytest.raises(ValidationError):
        TrialCheck(after_hours=[6, 12, 24, 48], conditions=cond)


def test_timers_must_get_later():
    with pytest.raises(ValidationError):
        TrialCheck(after_hours=[48, 24], conditions=[{"metric": "views", "minimum": 1}])
    with pytest.raises(ValidationError):
        TrialCheck(after_hours=[24, 24], conditions=[{"metric": "views", "minimum": 1}])


def test_a_miss_before_the_last_timer_keeps_the_reel_in_the_running():
    docs = [_two_timers()]
    _, sent = _run(docs, LOW)
    doc = docs[0]
    assert doc["trial_check_state"] == PENDING and doc["trial_check_stage"] == 1 and sent == []
    assert doc["trial_check_due_at"] == PUBLISHED + timedelta(hours=48)


def test_missing_the_last_timer_marks_it_missed():
    docs = [_two_timers(trial_check_stage=1, trial_check_due_at=NOW - timedelta(minutes=1))]
    _, sent = _run(docs, LOW)
    assert docs[0]["trial_check_state"] == MISSED and sent == []


def test_clearing_the_bar_at_the_last_timer_still_emails():
    docs = [_two_timers(trial_check_stage=1, trial_check_due_at=NOW - timedelta(minutes=1))]
    _, sent = _run(docs)
    assert docs[0]["trial_check_state"] == MET and len(sent) == 1
    assert docs[0]["trial_check_met_after_hours"] == 48


def test_clearing_the_bar_early_ends_it_without_waiting_for_the_rest():
    docs = [_two_timers()]
    _, sent = _run(docs)
    assert docs[0]["trial_check_state"] == MET and docs[0]["trial_check_met_after_hours"] == 24
    assert len(sent) == 1


def test_a_check_saved_with_one_number_still_reads_as_one_timer():
    docs = [_video(published_at=PUBLISHED)]
    _run(docs, LOW)
    assert docs[0]["trial_check_state"] == MISSED


# --- groups and comparisons ---------------------------------------------------

# (skip rate under 50% AND over 500 views) OR (over 2000 views) OR (over 50 saves)
NESTED: dict[str, Any] = {
    "after_hours": [24],
    "groups": [
        {
            "conditions": [
                {"metric": "skip_rate", "op": "lte", "value": 50},
                {"metric": "views", "op": "gte", "value": 500},
            ]
        },
        {"conditions": [{"metric": "views", "op": "gte", "value": 2000}]},
        {"conditions": [{"metric": "saves", "op": "gte", "value": 50}]},
    ],
}


def test_a_group_needs_all_of_its_conditions():
    assert evaluate(NESTED, {"skip_rate": 40, "views": 600}).met
    assert not evaluate(NESTED, {"skip_rate": 60, "views": 600}).met
    assert not evaluate(NESTED, {"skip_rate": 40, "views": 400}).met


def test_any_one_group_is_enough():
    assert evaluate(NESTED, {"skip_rate": 90, "views": 2500}).met
    assert evaluate(NESTED, {"skip_rate": 90, "views": 10, "saves": 60}).met
    assert not evaluate(NESTED, {"skip_rate": 90, "views": 10, "saves": 10}).met


def test_at_most_is_inclusive_and_a_missing_number_cannot_pass_it():
    check = {"after_hours": [24], "groups": [{"conditions": [{"metric": "skip_rate", "op": "lte", "value": 50}]}]}
    assert evaluate(check, {"skip_rate": 50}).met
    assert not evaluate(check, {"views": 9000}).met


def test_each_group_reports_its_own_result():
    outcome = evaluate(NESTED, {"skip_rate": 40, "views": 600})
    assert [g.passed for g in outcome.groups] == [True, False, False]


def test_a_check_needs_at_least_one_group_and_no_empty_group():
    with pytest.raises(ValidationError):
        TrialCheck(after_hours=[24], groups=[])
    with pytest.raises(ValidationError):
        TrialCheck(after_hours=[24], groups=[{"conditions": []}])


def test_an_older_all_check_becomes_one_group():
    old = {
        "after_hours": 24,
        "match": "all",
        "conditions": [{"metric": "views", "minimum": 1}, {"metric": "saves", "minimum": 2}],
    }
    new = upgrade_check(old)
    assert len(new["groups"]) == 1 and len(new["groups"][0]["conditions"]) == 2


def test_an_older_any_check_becomes_one_group_per_condition():
    old = {
        "after_hours": [24],
        "match": "any",
        "conditions": [{"metric": "views", "minimum": 1}, {"metric": "saves", "minimum": 2}],
    }
    assert [len(g["conditions"]) for g in upgrade_check(old)["groups"]] == [1, 1]


def test_an_older_check_gives_the_same_answer_as_before():
    any_check = {
        "after_hours": 24,
        "match": "any",
        "conditions": [{"metric": "views", "minimum": 1000}, {"metric": "saves", "minimum": 50}],
    }
    assert evaluate(any_check, {"views": 10, "saves": 60}).met
    assert not evaluate(any_check, {"views": 10, "saves": 10}).met


def test_the_email_names_which_option_passed():
    outcome = evaluate(NESTED, {"skip_rate": 40, "views": 600})
    mail = format_graduate_email(title="t", channel="c", permalink=None, after_hours=24, groups=outcome.groups)
    assert "Option 1: met" in mail.text and "Option 2: not met" in mail.text
    assert "Skip rate (%): 40 (needed at most 50)" in mail.text


# --- editing a check that is already running ----------------------------------


def _live(**over: Any) -> dict[str, Any]:
    check = {"after_hours": [24, 48], "groups": [{"conditions": [{"metric": "views", "op": "gte", "value": 1000}]}]}
    doc = {
        "trial_graduation": "MANUAL",
        "trial_check": check,
        "trial_check_state": PENDING,
        "trial_check_stage": 0,
        "published_at": PUBLISHED,
    }
    doc.update(over)
    return doc


def _new(hours: list[float], value: float = 5000) -> TrialCheck:
    return TrialCheck(after_hours=hours, groups=[{"conditions": [{"metric": "views", "op": "gte", "value": value}]}])


def test_a_running_check_can_change_its_conditions_and_timers():
    fields = edit_running_check(_live(), _new([12, 72], 5000))
    assert fields["trial_check"]["after_hours"] == [12, 72]
    assert fields["trial_check"]["groups"][0]["conditions"][0]["value"] == 5000
    assert fields["trial_check_due_at"] == PUBLISHED + timedelta(hours=12)


def test_editing_after_the_first_check_keeps_it_and_moves_the_next_one():
    video = _live(trial_check_stage=1)
    fields = edit_running_check(video, _new([24, 96]))
    assert fields["trial_check_due_at"] == PUBLISHED + timedelta(hours=96)


def test_a_check_that_already_ran_cannot_be_changed():
    with pytest.raises(ValueError, match="already run"):
        edit_running_check(_live(trial_check_stage=1), _new([12, 48]))


def test_at_least_one_check_must_remain():
    with pytest.raises(ValueError, match="at least one"):
        edit_running_check(_live(trial_check_stage=1), _new([24]))


@pytest.mark.parametrize("state", ["met", "missed", "failed"])
def test_a_finished_check_cannot_be_edited(state):
    with pytest.raises(ValueError, match="finished"):
        edit_running_check(_live(trial_check_state=state), _new([24, 48]))


def test_a_reel_without_a_check_has_nothing_to_edit():
    with pytest.raises(ValueError, match="no graduate check"):
        edit_running_check(_live(trial_check=None), _new([24]))
    with pytest.raises(ValueError, match="no graduate check"):
        edit_running_check(_live(trial_graduation="SS_PERFORMANCE"), _new([24]))


def test_an_email_still_owed_for_an_older_flat_result_is_still_sent():
    docs = [
        _video(
            trial_check_state=MET,
            trial_check_due_at=None,
            trial_check_result={
                "met": True,
                "results": [{"metric": "views", "minimum": 1000, "actual": 2000, "passed": True}],
            },
        )
    ]
    _, sent = _run(docs)
    assert len(sent) == 1


# --- the email's look ---------------------------------------------------------


def _mail(**over: Any):
    outcome = evaluate(NESTED, {"skip_rate": 40, "views": 600, "saves": 3})
    args: dict[str, Any] = {
        "title": "Sorting",
        "channel": "TryAlgoViz",
        "permalink": "https://www.instagram.com/reel/abc/",
        "after_hours": 24,
        "groups": outcome.groups,
    }
    args.update(over)
    return format_graduate_email(**args)


def test_the_email_shows_the_thumbnail_when_there_is_one():
    mail = _mail(thumbnail_url="https://cdn.example/t.jpg?a=1&b=2")
    assert 'src="https://cdn.example/t.jpg?a=1&amp;b=2"' in mail.html


def test_the_email_has_no_broken_image_without_a_thumbnail():
    assert "<img" not in _mail().html


def test_the_email_shows_the_headline_numbers_that_were_reported():
    mail = _mail(metrics={"views": 12345, "skip_rate": 41.3, "saves": 7})
    assert "12,345" in mail.html and "41.3%" in mail.html
    assert "Shares" not in mail.html.split("What it had to reach")[0]
    assert "Views: 12,345" in mail.text


def test_a_long_caption_is_cut_down_and_escaped():
    mail = _mail(title="<b>" + "word " * 100)
    assert "<b>" not in mail.html and "&lt;b&gt;" in mail.html
    assert len(mail.subject) < 100


def test_the_button_is_replaced_by_a_note_when_there_is_no_link():
    mail = _mail(permalink=None)
    assert "Open the reel in Instagram</a>" not in mail.html
    assert "No link was available" in mail.html
