"""Email delivery (graceful when unconfigured) and the summary formatter."""

from types import SimpleNamespace

import pytest

from app.services.auto_scheduler_summary import format_summary_email
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
