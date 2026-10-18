"""When an unticked story earns a second email."""

from datetime import datetime, timedelta, timezone

from app.services import story_kit as kit

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _video(**over):
    base = {
        "video_id": "v",
        "story_slug": "s",
        "status": "published",
        "scheduled_at": (NOW - timedelta(minutes=5)).replace(tzinfo=None),
    }
    return {**base, **over}


def _ago(**delta):
    return (NOW - timedelta(**delta)).replace(tzinfo=None)


def test_go_live_email_is_due_once_a_normal_reel_is_live():
    assert kit.go_live_due([_video()], NOW)


def test_go_live_email_waits_for_publishing_and_skips_trials_ticked_and_sent():
    assert not kit.go_live_due([_video(status="scheduled")], NOW)
    assert not kit.go_live_due([_video(trial_graduation="MANUAL")], NOW)
    assert not kit.go_live_due([_video(story_scheduled_at=NOW)], NOW)
    assert not kit.go_live_due([_video(story_live_emailed_at=NOW)], NOW)
    assert not kit.go_live_due([_video(story_slug=None)], NOW)


def test_a_reel_live_for_hours_gets_no_stale_prompt():
    assert not kit.go_live_due([_video(scheduled_at=_ago(hours=7))], NOW)


def test_normal_reel_reminder_comes_two_and_a_half_hours_after_the_live_email():
    assert not kit.reminders_due([_video()], NOW)  # never emailed
    assert not kit.reminders_due([_video(story_live_emailed_at=_ago(hours=2))], NOW)
    assert kit.reminders_due([_video(story_live_emailed_at=_ago(hours=3))], NOW)


def test_ticked_or_already_reminded_is_left_alone():
    sent = _ago(hours=3)
    assert not kit.reminders_due([_video(story_live_emailed_at=sent, story_scheduled_at=NOW)], NOW)
    assert not kit.reminders_due([_video(story_live_emailed_at=sent, story_reminded_at=NOW)], NOW)
    assert not kit.reminders_due([_video(story_live_emailed_at=sent, status="archived")], NOW)


def test_trial_waits_for_graduation_plus_three_hours():
    trial = {"trial_graduation": "MANUAL", "trial_check_state": "met"}
    fresh = _video(**trial, trial_check_checked_at=_ago(hours=1))
    old = _video(**trial, trial_check_checked_at=_ago(hours=4))
    assert not kit.reminders_due([fresh], NOW)
    assert kit.reminders_due([old], NOW)


def test_trial_that_has_not_graduated_never_gets_one():
    pending = _video(trial_graduation="MANUAL", trial_check_state="pending")
    assert not kit.reminders_due([pending], NOW)


def test_nothing_after_the_page_has_dropped_the_reel():
    stale = _video(story_live_emailed_at=_ago(days=4))
    assert not kit.reminders_due([stale], NOW)


def test_emails_say_what_they_are():
    item = kit.KitItem("v", "s", "A reel", NOW)
    subject, text, _ = kit.kit_email([item], "https://x/s/t")
    assert subject == "Post this story now: 1 reel" and "just went live" in text
    subject, text, _ = kit.kit_email([item], "https://x/s/t", reminder=True)
    assert subject.startswith("Story not posted yet") and "not ticked as posted" in text


def test_send_story_emails_prompts_then_reminds_each_exactly_once(monkeypatch):
    import asyncio

    from app.services import story_reminder

    async def page(db, channel_id):
        return "https://x/s/token"

    monkeypatch.setattr("app.routers.story_kit.channel_page_url", page)
    doc = {"_id": 1, "channel_id": "c", **_video()}

    class Cursor:
        def __init__(self, stamp):
            self.stamp = stamp

        async def to_list(self, length=None):
            return [] if self.stamp in doc else [doc]

    class Videos:
        def find(self, q):
            stamp = next(k for k in q if k in ("story_live_emailed_at", "story_reminded_at", "story_flip_emailed_at"))
            return Cursor(stamp)

        async def update_one(self, key, update):
            doc.update(update["$set"])

    sent = []

    async def send(subject, text, html):
        sent.append(subject)
        return True

    db = type("Db", (), {"videos": Videos()})()
    run = lambda at: asyncio.run(story_reminder.send_story_emails(db, send, at))  # noqa: E731
    assert run(NOW) == 1 and sent == ["Post this story now: 1 reel"]
    assert run(NOW + timedelta(hours=1)) == 0
    assert run(NOW + timedelta(hours=3)) == 1 and sent[-1] == "Story not posted yet: 1 reel"
    assert run(NOW + timedelta(hours=6)) == 0 and len(sent) == 2


# --- a trial seen joining the grid -------------------------------------------


def _trial(**over):
    return _video(**{"trial_graduation": "SS_PERFORMANCE", "graduated_at": _ago(minutes=30), **over})


def test_a_trial_that_joined_the_grid_gets_a_story_email():
    assert kit.flip_due([_trial()], NOW)


def test_the_flip_email_skips_what_the_graduation_email_already_covered():
    assert not kit.flip_due([_trial(trial_check_emailed=True)], NOW)
    assert not kit.flip_due([_trial(story_flip_emailed_at=NOW)], NOW)
    assert not kit.flip_due([_trial(story_scheduled_at=NOW)], NOW)
    assert not kit.flip_due([_video()], NOW)  # not a trial
    assert not kit.flip_due([_video(trial_graduation="MANUAL")], NOW)  # never seen on the grid


def test_a_flip_noticed_hours_late_gets_no_prompt():
    assert not kit.flip_due([_trial(graduated_at=_ago(hours=7))], NOW)


def test_a_flipped_trial_is_reminded_three_hours_on_and_listed_on_the_page():
    old = _trial(graduated_at=_ago(hours=4))
    assert kit.reminders_due([old], NOW)
    assert [i.graduated for i in kit.page_items([old], NOW)] == [True]


def test_the_graduated_email_says_so():
    subject, text, html = kit.kit_email([kit.KitItem("v", "s", "A reel", NOW)], "https://x/s/t", graduated=True)
    assert subject == "Post this story now: 1 reel" and "just graduated to followers" in text
    assert "Graduated to followers" in html and "live since" not in text
