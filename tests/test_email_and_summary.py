"""Email delivery (graceful when unconfigured) and the summary formatter."""

from types import SimpleNamespace

import pytest

from app.services.auto_scheduler_summary import format_duration, format_summary_email
from app.services.email_service import build_message, send_email


def _settings(**over):
    base = dict(
        SMTP_HOST=None,
        SMTP_PORT=587,
        SMTP_USER=None,
        SMTP_PASSWORD=None,
        SMTP_FROM=None,
        SMTP_USE_TLS=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ------------------------------------------------------------------
# email
# ------------------------------------------------------------------


def test_build_message_sets_headers_and_body():
    msg = build_message("from@x.com", "to@y.com", "Subj", "Hello")
    assert msg["From"] == "from@x.com"
    assert msg["To"] == "to@y.com"
    assert msg["Subject"] == "Subj"
    assert "Hello" in msg.get_content()


@pytest.mark.asyncio
async def test_send_is_skipped_and_reported_false_when_smtp_unconfigured():
    sent = await send_email(_settings(), "to@y.com", "Subj", "Body")
    assert sent is False


@pytest.mark.asyncio
async def test_send_is_skipped_when_no_recipient():
    sent = await send_email(_settings(SMTP_HOST="smtp.x.com", SMTP_FROM="from@x.com"), None, "Subj", "Body")
    assert sent is False


@pytest.mark.asyncio
async def test_send_uses_the_smtp_path_when_configured(monkeypatch):
    calls = {}

    def fake_send_sync(settings, msg):
        calls["to"] = msg["To"]
        calls["subject"] = msg["Subject"]

    monkeypatch.setattr("app.services.email_service._send_sync", fake_send_sync)
    sent = await send_email(
        _settings(SMTP_HOST="smtp.x.com", SMTP_FROM="from@x.com"),
        "to@y.com",
        "Daily",
        "Body",
    )
    assert sent is True
    assert calls == {"to": "to@y.com", "subject": "Daily"}


@pytest.mark.asyncio
async def test_send_swallows_smtp_errors_and_returns_false(monkeypatch):
    def boom(settings, msg):
        raise OSError("connection refused")

    monkeypatch.setattr("app.services.email_service._send_sync", boom)
    sent = await send_email(_settings(SMTP_HOST="smtp.x.com", SMTP_FROM="from@x.com"), "to@y.com", "S", "B")
    assert sent is False


# ------------------------------------------------------------------
# summary formatting
# ------------------------------------------------------------------


def test_summary_subject_reports_counts_and_date():
    summary = {
        "date": "2026-08-24",
        "scheduled": [{"channel_id": "c1", "slot": "19:00", "video_title": "A clip"}],
        "skipped": [
            {"channel_id": "c2", "slot": "19:00", "reason": "import not configured"},
            {"channel_id": "c3", "slot": "19:00", "reason": "no videos available"},
        ],
    }
    email = format_summary_email(summary)
    assert "1" in email.subject and "2" in email.subject
    assert "2026-08-24" in email.subject


def test_both_bodies_list_every_channel_outcome():
    """Neither body may hide a fact the other shows."""
    summary = {
        "date": "2026-08-24",
        "scheduled": [{"channel_name": "Histriphy", "slot": "19:00", "video_title": "A clip"}],
        "skipped": [{"channel_name": "Otherchan", "slot": "21:00", "reason": "import not configured"}],
    }
    email = format_summary_email(summary)
    for body in (email.text, email.html):
        assert "Histriphy" in body
        assert "Otherchan" in body
        assert "import not configured" in body


def test_summary_handles_a_run_with_nothing_to_do():
    email = format_summary_email({"date": "2026-08-24", "scheduled": [], "skipped": []})
    assert "0" in email.subject
    assert "Nothing was scheduled today." in email.html
    assert "(none)" in email.text


def test_video_ids_never_reach_the_reader():
    """A uuid tells a person nothing — the title is what they need."""
    summary = {
        "date": "2026-08-24",
        "scheduled": [
            {
                "channel_name": "Geo Ranking",
                "slot": "19:00",
                "video_id": "cc75dd23-988d-43c8-98d9-49ed3346a1e0",
                "video_title": "Why Canada has 60% of the World's Lakes",
            }
        ],
        "skipped": [],
    }
    email = format_summary_email(summary)
    for body in (email.text, email.html):
        assert "cc75dd23" not in body
        assert "Canada" in body


def test_a_channel_picture_is_shown_when_there_is_one():
    summary = {
        "date": "2026-08-24",
        "scheduled": [
            {"channel_name": "Geo Ranking", "slot": "19:00", "video_title": "x",
             "channel_thumbnail": "https://cdn.example.com/geo.jpg"}
        ],
        "skipped": [],
    }
    html = format_summary_email(summary).html
    assert 'src="https://cdn.example.com/geo.jpg"' in html
    # Remote images are commonly blocked, so the name must survive without it.
    assert 'alt="Geo Ranking"' in html


def test_a_channel_without_a_picture_gets_an_initial_not_a_broken_image():
    summary = {
        "date": "2026-08-24",
        "scheduled": [{"channel_name": "Geo Ranking", "slot": "19:00", "video_title": "x"}],
        "skipped": [],
    }
    html = format_summary_email(summary).html
    assert "<img" not in html
    assert ">G<" in html


def test_the_same_channel_always_gets_the_same_stand_in_colour():
    """A colour that changed between emails would read as a different channel."""
    from app.services.auto_scheduler_summary import _avatar_colour

    assert _avatar_colour("Geo Ranking") == _avatar_colour("Geo Ranking")


def test_titles_with_html_characters_cannot_break_the_layout():
    """A real title carrying < or & must render as text, not markup."""
    summary = {
        "date": "2026-08-24",
        "scheduled": [
            {"channel_name": "A & B <Media>", "slot": "19:00", "video_title": "5 < 10 & rising"}
        ],
        "skipped": [],
    }
    html = format_summary_email(summary).html
    assert "<Media>" not in html
    assert "&lt;Media&gt;" in html
    assert "5 &lt; 10 &amp; rising" in html


def test_a_missing_title_falls_back_to_a_word_not_a_blank():
    summary = {
        "date": "2026-08-24",
        "scheduled": [{"channel_name": "Geo Ranking", "slot": "19:00"}],
        "skipped": [],
    }
    email = format_summary_email(summary)
    assert "Untitled" in email.html
    assert "Untitled" in email.text


def test_the_html_is_self_contained():
    """Mail clients strip <style> and block scripts; everything must be inline."""
    html = format_summary_email(
        {"date": "2026-08-24", "scheduled": [{"channel_name": "C", "slot": "1", "video_title": "t"}], "skipped": []}
    ).html
    assert "<style" not in html
    assert "<script" not in html
    assert "class=" not in html


# ------------------------------------------------------------------
# per-video timing
# ------------------------------------------------------------------
#
# The digest says what was posted; these say how long it took to make. That is the
# question the near-misses kept raising — a video that made its slot by seconds
# looks identical to a comfortable one until the time is on the page.


def test_format_duration_reads_naturally_at_every_scale():
    assert format_duration(0) == "0s"
    assert format_duration(48) == "48s"
    assert format_duration(59.4) == "59s"
    # Seconds are kept below the hour: a 40s import and a 4m one are different stories.
    assert format_duration(60) == "1m 00s"
    assert format_duration(544) == "9m 04s"
    assert format_duration(3599) == "59m 59s"
    # ...and dropped past it, where they stop mattering.
    assert format_duration(3600) == "1h 00m"
    assert format_duration(4329) == "1h 12m"


def _scheduled(**over):
    row = {"channel_id": "geo", "channel_name": "Geo Ranking", "slot": "19:00", "video_title": "Raisi"}
    row.update(over)
    return {"date": "2026-09-24", "scheduled": [row], "skipped": []}


def test_a_rendered_video_reports_its_total_and_the_split():
    summary = _scheduled(
        source="rendered by GeoRank renderer",
        timing={"total_seconds": 750.0, "render_seconds": 544.0, "import_seconds": 206.0},
    )
    email = format_summary_email(summary)
    for body in (email.text, email.html):
        assert "Ready in 12m 30s" in body
        assert "render 9m 04s" in body
        assert "import 3m 26s" in body


def test_a_split_that_is_only_half_known_shows_the_total_alone():
    """A bare total beats a breakdown that does not add up."""
    summary = _scheduled(source="GeoRank", timing={"total_seconds": 206.0, "import_seconds": 206.0})
    email = format_summary_email(summary)
    assert "Ready in 3m 26s" in email.text
    assert "render" not in email.text


def test_a_late_linked_copy_says_how_long_it_was_held_up():
    """The Instagram gap in plain sight: the copy went out, and this is the lag."""
    summary = _scheduled(
        source="linked to Geo Ranking",
        timing={"total_seconds": 750.0, "render_seconds": 544.0, "import_seconds": 206.0},
        waited_seconds=372.0,
    )
    email = format_summary_email(summary)
    for body in (email.text, email.html):
        assert "waited 6m 12s to be postable" in body


def test_an_on_time_copy_is_not_labelled_as_having_waited():
    summary = _scheduled(source="linked to Geo Ranking", timing={"total_seconds": 750.0}, waited_seconds=0.0)
    assert "waited" not in format_summary_email(summary).text


def test_a_video_taken_from_ready_says_so_rather_than_showing_a_blank():
    """It was produced on an earlier day, so reporting the days it sat waiting as
    generation time would be worse than saying nothing — but silence reads like a
    missing number, so the absence is named."""
    summary = _scheduled(timing=None)
    email = format_summary_email(summary)
    assert "Picked from Ready" in email.text
    assert "Ready in" not in email.text


def test_an_untimed_import_row_stays_quiet_rather_than_guessing():
    """Rows scheduled before timing was recorded have a source but no stamps."""
    summary = _scheduled(source="GeoRank renderer", timing=None)
    email = format_summary_email(summary)
    assert "Picked from Ready" not in email.text
    assert "Ready in" not in email.text
    assert "via GeoRank renderer" in email.text  # the row itself still renders


def test_timing_never_appears_on_a_skipped_row():
    """A skipped slot has a reason, not a duration; the row shows the reason."""
    summary = {
        "date": "2026-09-24",
        "scheduled": [],
        "skipped": [{"channel_id": "geo", "slot": "19:00", "reason": "import not ready in time", "timing": None}],
    }
    email = format_summary_email(summary)
    assert "import not ready in time" in email.text
    assert "Picked from Ready" not in email.html


# ------------------------------------------------------------------
# The app's remarks
# ------------------------------------------------------------------
#
# Everything else in this digest records what happened. A remark means something
# needs fixing at the other end — a format with no episode left to air keeps
# falling back to a standard video silently and indefinitely, and every post looks
# fine while it does. So it gets the top of the email and the only red on the page.

_NO_EPISODE = "Scheduled format has no next episode to air (no ideated series) — serving the best available video."


def _with_remarks(*remarks):
    return {
        "date": "2026-09-27",
        "scheduled": [{"channel_id": "geo", "channel_name": "Geo Ranking", "slot": "19:00", "video_title": "Hormuz"}],
        "skipped": [],
        "remarks": list(remarks),
    }


def _remark(**over):
    base = {"channel_id": "geo", "channel_name": "Geo Ranking", "slot": "19:00", "source": "GeoRank renderer"}
    base.update(over)
    return base


def test_a_remark_is_announced_in_the_subject_line():
    """Visible in the inbox list, before anything is opened."""
    email = format_summary_email(_with_remarks(_remark(remark=_NO_EPISODE)))
    assert email.subject.startswith("⚠️")
    assert "1 remark" in email.subject


def test_several_remarks_are_counted_and_pluralised():
    email = format_summary_email(_with_remarks(_remark(remark="a"), _remark(remark="b", slot="21:00")))
    assert "2 remarks" in email.subject


def test_a_clean_day_keeps_the_plain_subject_and_shows_no_banner():
    """The signal only works if a quiet day is visibly quiet."""
    email = format_summary_email(_with_remarks())
    assert email.subject == "Auto-scheduler: 1 scheduled, 0 skipped (2026-09-27)"
    assert "⚠" not in email.subject
    assert "from the app" not in email.html
    assert "FROM THE APP" not in email.text


def test_the_remark_appears_in_both_bodies_with_who_it_concerns():
    email = format_summary_email(_with_remarks(_remark(remark=_NO_EPISODE)))
    for body in (email.text, email.html):
        assert "no next episode to air" in body
        assert "Geo Ranking" in body
        assert "19:00" in body
        assert "GeoRank renderer" in body


def test_the_banner_comes_before_the_counts_it_would_otherwise_hide_behind():
    html = format_summary_email(_with_remarks(_remark(remark=_NO_EPISODE))).html
    assert html.index("from the app") < html.index("Scheduled</div>")


def test_the_banner_is_the_only_red_and_not_the_amber_used_for_a_skip():
    """A skipped slot is amber; this is red, because it is the one thing asking the
    reader to go and fix something."""
    html = format_summary_email(_with_remarks(_remark(remark=_NO_EPISODE))).html
    assert "#fee2e2" in html  # alert background
    assert "#b91c1c" in html  # alert ink


def test_a_remark_is_not_lost_when_the_video_posted_perfectly():
    """The case a per-row note would hide: nothing was skipped, so the reader has
    no reason to look past the counts — the banner has to reach them anyway."""
    summary = _with_remarks(_remark(remark=_NO_EPISODE))
    email = format_summary_email(summary)
    assert summary["skipped"] == []
    assert "no next episode to air" in email.text


def test_a_remark_is_escaped_rather_than_trusted():
    """It is text from another service, rendered into our HTML."""
    html = format_summary_email(_with_remarks(_remark(remark="<script>alert(1)</script>"))).html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_remark_without_a_channel_name_still_renders():
    email = format_summary_email(_with_remarks({"channel_id": "geo", "slot": "19:00", "remark": "terse"}))
    assert "terse" in email.text
    assert "geo" in email.text
