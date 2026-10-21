"""The owner hears about a publish that gave up, in the platform's own words."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
import requests

from app.services import publish_failure_notify
from app.services.publish_failure import build_failure_marker, platform_message
from app.services.publish_failure_email import format_failure_email

META_BODY = {
    "error": {
        "message": "The caption was too long.",
        "code": 36004,
        "error_subcode": 2207117,
        "error_user_title": "Too many hashtags used",
        "error_user_msg": "The submitted caption has too many hashtags. The maximum number of hashtags permitted for a caption is 30.",
    }
}


class _Response:
    def __init__(self, body: Any) -> None:
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _http_error(body: Any) -> requests.HTTPError:
    return requests.HTTPError("400 Client Error: Bad Request", response=_Response(body))  # type: ignore[arg-type]


# --- the platform's words -----------------------------------------------------


def test_the_platforms_title_and_message_are_read_off_the_response():
    said = platform_message(_http_error(META_BODY))
    assert said and said.startswith("Too many hashtags used: ") and "maximum number of hashtags" in said


def test_a_response_without_json_gives_nothing():
    assert platform_message(_http_error(ValueError("not json"))) is None
    assert platform_message(RuntimeError("boom")) is None


def test_the_failure_marker_keeps_the_platforms_words_not_just_bad_request():
    marker = build_failure_marker(stage="publish", platform="instagram", attempts=5, exc=_http_error(META_BODY))
    assert "Too many hashtags used" in marker["detail"]


def test_without_platform_words_the_marker_keeps_the_exception_text():
    marker = build_failure_marker(stage="upload", platform="youtube", attempts=5, exc=RuntimeError("disk full"))
    assert marker["detail"] == "disk full"


# --- the email ----------------------------------------------------------------


def _mail(**over: Any):
    args: dict[str, Any] = {
        "title": "Why Spain Owns Two Cities",
        "channel": "Geo Ranking",
        "platform": "instagram",
        "stage": "publish",
        "reason": "Upload or publish failed",
        "detail": "Too many hashtags used: the maximum is 30.",
        "attempts": 5,
        "scheduled_at": datetime(2026, 10, 21, 7, 0),
        "app_url": "https://app.example",
    }
    args.update(over)
    return format_failure_email(**args)


def test_the_email_leads_with_what_the_platform_said():
    mail = _mail()
    assert "Too many hashtags used" in mail.text and "Too many hashtags used" in mail.html
    assert mail.subject == "Reel did not publish: Why Spain Owns Two Cities (Geo Ranking)"


def test_a_youtube_failure_says_upload_not_publish():
    mail = _mail(platform="youtube", stage="upload")
    assert mail.subject.startswith("Video did not upload:")


def test_detail_that_only_repeats_the_reason_is_not_shown_twice():
    mail = _mail(reason="Platform quota or rate limit reached", detail="Platform quota or rate limit reached")
    assert "Platform said" not in mail.text


def test_the_email_escapes_what_it_was_given():
    mail = _mail(title="<script>x</script>", detail="<b>bad</b>")
    assert "<script>" not in mail.html and "<b>bad</b>" not in mail.html


def test_the_email_says_when_it_was_due_and_how_often_it_tried():
    mail = _mail()
    assert "21 Oct, 07:00 IST" in mail.text and "5 times" in mail.text


# --- sending it never breaks the worker ---------------------------------------


class _Channels:
    async def find_one(self, *_a, **_k):
        return {"name": "Geo Ranking"}


class _DB:
    channels = _Channels()
    profiles = None


def _notify(monkeypatch: pytest.MonkeyPatch, send: Any) -> bool:
    async def owner(_db, _settings):
        return "owner@example.com"

    monkeypatch.setattr(publish_failure_notify, "send_email", send)
    monkeypatch.setattr(publish_failure_notify, "resolve_owner_email", owner)
    marker = build_failure_marker(stage="publish", platform="instagram", attempts=5, exc=_http_error(META_BODY))
    video = {"channel_id": "c1", "video_id": "v1", "title": "T"}
    return asyncio.run(publish_failure_notify.notify_publish_failure(_DB(), video, marker, None))


def test_the_owner_is_emailed_the_platforms_reason(monkeypatch):
    sent: list[str] = []

    async def send(_settings, to, subject, text, _html):
        sent.append(f"{to}|{subject}|{'hashtags' in text}")
        return True

    assert _notify(monkeypatch, send) is True
    assert sent == ["owner@example.com|Reel did not publish: T (Geo Ranking)|True"]


def test_a_failing_mail_server_does_not_raise(monkeypatch):
    async def send(*_a, **_k):
        raise OSError("smtp down")

    assert _notify(monkeypatch, send) is False


def test_mail_that_is_not_configured_is_reported_as_unsent(monkeypatch):
    async def send(*_a, **_k):
        return False

    assert _notify(monkeypatch, send) is False


def test_a_vague_reason_gives_way_to_the_platforms_own_words():
    mail = _mail(reason="Upload or publish failed", detail="Too many hashtags used: the maximum is 30.")
    assert "What went wrong: Too many hashtags used: the maximum is 30." in mail.text
    assert "Upload or publish failed" not in mail.text and "Platform said" not in mail.text
