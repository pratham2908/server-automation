"""When an unticked story earns a second email."""

from datetime import datetime, timedelta, timezone

from app.services import story_kit as kit

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _video(**over):
    base = {"video_id": "v", "story_slug": "s", "scheduled_at": (NOW - timedelta(minutes=5)).replace(tzinfo=None)}
    return {**base, **over}


def test_normal_reel_is_due_once_live_and_unticked():
    assert kit.reminders_due([_video()], NOW)


def test_normal_reel_not_due_before_it_is_live():
    assert not kit.reminders_due([_video(scheduled_at=(NOW + timedelta(hours=1)).replace(tzinfo=None))], NOW)


def test_ticked_or_already_reminded_is_left_alone():
    assert not kit.reminders_due([_video(story_scheduled_at=NOW)], NOW)
    assert not kit.reminders_due([_video(story_reminded_at=NOW)], NOW)


def test_reel_without_story_or_archived_is_left_alone():
    assert not kit.reminders_due([_video(story_slug=None)], NOW)
    assert not kit.reminders_due([_video(status="archived")], NOW)


def test_trial_waits_for_graduation_plus_three_hours():
    trial = {"trial_graduation": "MANUAL", "trial_check_state": "met"}
    fresh = _video(**trial, trial_check_checked_at=(NOW - timedelta(hours=1)).replace(tzinfo=None))
    old = _video(**trial, trial_check_checked_at=(NOW - timedelta(hours=4)).replace(tzinfo=None))
    assert not kit.reminders_due([fresh], NOW)
    assert kit.reminders_due([old], NOW)


def test_trial_that_has_not_graduated_never_gets_one():
    pending = _video(trial_graduation="MANUAL", trial_check_state="pending")
    assert not kit.reminders_due([pending], NOW)


def test_nothing_after_the_page_has_dropped_the_reel():
    stale = _video(scheduled_at=(NOW - timedelta(days=4)).replace(tzinfo=None))
    assert not kit.reminders_due([stale], NOW)


def test_reminder_email_says_so():
    subject, text, _ = kit.kit_email([kit.KitItem("v", "s", "A reel", NOW)], "https://x/s/t", reminder=True)
    assert subject.startswith("Story not scheduled yet") and "not ticked" in text


def test_send_story_reminders_emails_once_then_marks(monkeypatch):
    import asyncio

    from app.services import story_reminder

    async def page(db, channel_id):
        return "https://x/s/token"

    monkeypatch.setattr("app.routers.story_kit.channel_page_url", page)
    doc = {"_id": 1, "channel_id": "c", **_video()}

    class Cursor:
        async def to_list(self, length=None):
            return [d for d in [doc] if "story_reminded_at" not in d]

    class Videos:
        def find(self, q):
            return Cursor()

        async def update_one(self, key, update):
            doc.update(update["$set"])

    sent = []

    async def send(subject, text, html):
        sent.append(subject)
        return True

    db = type("Db", (), {"videos": Videos()})()
    assert asyncio.run(story_reminder.send_story_reminders(db, send, NOW)) == 1
    assert asyncio.run(story_reminder.send_story_reminders(db, send, NOW)) == 0
    assert len(sent) == 1 and doc["story_reminded_at"] == NOW
