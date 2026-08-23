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
        "scheduled": [{"channel_id": "c1", "slot": "19:00", "video_id": "v1"}],
        "skipped": [
            {"channel_id": "c2", "slot": "19:00", "reason": "import not configured"},
            {"channel_id": "c3", "slot": "19:00", "reason": "no videos available"},
        ],
    }
    subject, body = format_summary_email(summary)
    assert "1" in subject and "2" in subject
    assert "2026-08-24" in subject


def test_summary_body_lists_every_channel_outcome():
    summary = {
        "date": "2026-08-24",
        "scheduled": [{"channel_id": "histriphy", "slot": "19:00", "video_id": "v1"}],
        "skipped": [{"channel_id": "otherchan", "slot": "21:00", "reason": "import not configured"}],
    }
    _subject, body = format_summary_email(summary)
    assert "histriphy" in body
    assert "otherchan" in body
    assert "import not configured" in body


def test_summary_handles_a_run_with_nothing_to_do():
    subject, body = format_summary_email({"date": "2026-08-24", "scheduled": [], "skipped": []})
    assert "0" in subject
    assert isinstance(body, str)
