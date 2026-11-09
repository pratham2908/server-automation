from datetime import date, datetime, timedelta

import pytest

from app.services.daily_digest import day_bounds, gain_over_day, totals_of, value_near, video_url
from app.services.daily_digest_email import compact, format_digest_email, signed
from app.timezone import IST

START, END = day_bounds(date(2026, 11, 8))


def at(minutes_from_start: int) -> datetime:
    return START + timedelta(minutes=minutes_from_start)


def test_day_bounds_are_ist_midnights():
    assert START == datetime(2026, 11, 8, tzinfo=IST) and END - START == timedelta(days=1)


def test_value_near_takes_the_closest_reading_within_tolerance():
    readings = [(at(-10), 100.0), (at(5), 105.0), (at(600), 200.0)]
    assert value_near(readings, START) == 105.0
    assert value_near(readings, START + timedelta(days=3)) is None


def test_gain_between_edges_and_missing_edge():
    readings = [(at(-5), 1000.0), (at(1445), 1900.0)]
    assert gain_over_day(readings, START, END) == 900.0
    assert gain_over_day([(at(1445), 1900.0)], START, END) is None


def test_post_born_in_the_day_started_at_zero():
    assert gain_over_day([(at(1445), 640.0)], START, END, born_in_day=True) == 640.0


def test_a_dip_is_not_reported_as_negative_gain():
    assert gain_over_day([(at(0), 500.0), (at(1440), 480.0)], START, END) == 0.0


def test_video_urls():
    assert video_url("youtube", {"youtube_video_id": "abc"}) == "https://www.youtube.com/watch?v=abc"
    assert video_url("instagram", {"instagram_media_id": "123"}) == "https://www.instagram.com/reel/123/"
    assert video_url("instagram", {}) is None


def test_totals_ignore_missing_figures():
    channels = [
        {"followers_change": 5, "views": None, "posts": [1]},
        {"followers_change": None, "views": 100, "posts": []},
    ]
    assert totals_of(channels) == {"channels": 2, "followers_change": 5, "views": 100, "posts": 1}
    assert totals_of([{"followers_change": None, "views": None, "posts": []}])["views"] is None


@pytest.mark.parametrize(
    ("value", "text"), [(None, "—"), (7, "7"), (1234, "1,234"), (12400, "12.4k"), (2_500_000, "2.5M"), (-30, "-30")]
)
def test_compact(value, text):
    assert compact(value) == text


def test_signed():
    assert signed(5) == "+5" and signed(0) == "0" and signed(-3) == "-3" and signed(None) == "—"


DIGEST = {
    "date": "2026-11-08",
    "totals": {"channels": 2, "followers_change": 212, "views": 48200, "posts": 3},
    "channels": [
        {
            "channel_id": "a",
            "name": "Why The <Map>",
            "handle": "@why_the_map",
            "platform": "instagram",
            "thumbnail_url": None,
            "followers": 14500,
            "followers_change": 180,
            "views": 40100,
            "posts": [{"title": "Capitals ranked", "url": "https://x.test/1"}],
            "top_posts": [{"title": "Capitals ranked", "views": 31000, "url": "https://x.test/1"}],
        },
        {
            "channel_id": "b",
            "name": "Physics ASMR",
            "handle": "@physicsasmr",
            "platform": "youtube",
            "thumbnail_url": None,
            "followers": 900,
            "followers_change": None,
            "views": None,
            "posts": [],
            "top_posts": [],
        },
    ],
}


def test_email_carries_the_facts_and_escapes_names():
    mail = format_digest_email(DIGEST, "https://app.test")
    assert "+212 followers" in mail.subject and "48.2k views" in mail.subject
    assert "Sunday 8 November" in mail.html
    assert "Why The &lt;Map&gt;" in mail.html and "<Map>" not in mail.html
    assert "Best day" in mail.html and "Capitals ranked" in mail.html
    assert (
        "Capitals ranked - 31.0k views" in mail.text or "Capitals ranked - 31000" in mail.text or "31.0k" in mail.text
    )
    assert "https://app.test" in mail.html and "https://app.test" in mail.text


def test_single_channel_has_no_best_day():
    one = {**DIGEST, "channels": DIGEST["channels"][:1], "totals": {**DIGEST["totals"], "channels": 1}}
    assert "Best day" not in format_digest_email(one, "https://app.test").html


# ---- the daily tick ---------------------------------------------------------------------


class _Digests:
    def __init__(self):
        self.doc = None

    async def update_one(self, query, update, upsert=False):
        if "$setOnInsert" in update and self.doc is None:
            self.doc = dict(update["$setOnInsert"])
        if "$set" in update and self.doc is not None:
            self.doc.update(update["$set"])

    async def find_one_and_update(self, query, update):
        d = self.doc
        if d is None or d["done"] or d["attempts"] >= query["attempts"]["$lt"]:
            return None
        before = dict(d)
        d["attempts"] += update["$inc"]["attempts"]
        return before


@pytest.mark.asyncio
async def test_tick_sends_once_then_stays_quiet(monkeypatch):
    from types import SimpleNamespace

    from app.services import daily_digest_cron as cron

    sent = []

    async def fake_send(db, settings, day):
        sent.append(day)
        return "sent"

    monkeypatch.setattr(cron, "send_digest", fake_send)
    monkeypatch.setattr(cron, "now_ist", lambda: datetime(2026, 11, 9, 8, 5, tzinfo=IST))
    db = SimpleNamespace(daily_digests=_Digests())
    assert await cron.run_due_digest(db, None) == "sent"
    assert await cron.run_due_digest(db, None) is None
    assert sent == [date(2026, 11, 8)]


@pytest.mark.asyncio
async def test_tick_waits_for_the_send_hour(monkeypatch):
    from types import SimpleNamespace

    from app.services import daily_digest_cron as cron

    monkeypatch.setattr(cron, "now_ist", lambda: datetime(2026, 11, 9, 7, 59, tzinfo=IST))
    assert await cron.run_due_digest(SimpleNamespace(daily_digests=_Digests()), None) is None


@pytest.mark.asyncio
async def test_failed_send_retries_then_gives_up(monkeypatch):
    from types import SimpleNamespace

    from app.services import daily_digest_cron as cron

    calls = []

    async def failing(db, settings, day):
        calls.append(1)
        return "not sent"

    monkeypatch.setattr(cron, "send_digest", failing)
    monkeypatch.setattr(cron, "now_ist", lambda: datetime(2026, 11, 9, 9, 0, tzinfo=IST))
    db = SimpleNamespace(daily_digests=_Digests())
    for _ in range(6):
        await cron.run_due_digest(db, None)
    assert len(calls) == cron.MAX_ATTEMPTS


def test_long_published_list_is_capped():
    many = [{"title": f"Post {i}", "url": None} for i in range(12)]
    digest = {**DIGEST, "channels": [{**DIGEST["channels"][0], "posts": many}]}
    mail = format_digest_email(digest, "https://app.test")
    assert "Published (12)" in mail.html and "and 7 more" in mail.html and "Post 6" not in mail.html
    assert "and 7 more published" in mail.text
