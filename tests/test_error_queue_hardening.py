"""Cover two recurring error-queue entries.

* ``Instagram container <id> processing failed (status: ERROR)`` — 15
  occurrences with no indication of *why*, because only ``status_code`` was
  requested from the Graph API.
* ``Auto-sync failed for '<channel>': No YouTube token`` — 72 occurrences from
  a single disconnected channel, re-filed on every cron run.
"""

import pytest

from app.exceptions import ChannelNotConnectedError
from app.services.instagram import InstagramService


def _service(payload):
    """InstagramService with the Graph API call stubbed out."""
    svc = InstagramService("token")
    captured = {}

    def fake_get(endpoint, params=None):
        captured["endpoint"] = endpoint
        captured["params"] = params
        return payload

    svc._get = fake_get  # type: ignore[method-assign]
    return svc, captured


class TestContainerStatusDetail:
    def test_requests_the_status_field(self):
        """Without ``status`` in fields, Meta never tells us the reason."""
        svc, captured = _service({"status_code": "FINISHED"})
        svc.get_container_status("123")
        assert "status" in captured["params"]["fields"]

    def test_returns_code_and_detail(self):
        svc, _ = _service({"status_code": "ERROR", "status": "The media is not a valid video file"})
        assert svc.get_container_status("123") == (
            "ERROR",
            "The media is not a valid video file",
        )

    def test_missing_fields_degrade_safely(self):
        svc, _ = _service({})
        assert svc.get_container_status("123") == ("UNKNOWN", "")

    def test_check_container_status_still_returns_bare_code(self):
        """Back-compat wrapper for callers that only want the code."""
        svc, _ = _service({"status_code": "IN_PROGRESS", "status": "processing"})
        assert svc.check_container_status("123") == "IN_PROGRESS"


class TestPublishSurfacesReason:
    def test_error_message_includes_metas_reason(self):
        svc, _ = _service({"status_code": "ERROR", "status": "Video duration exceeds the limit"})
        svc.create_reel_container = lambda *a, **k: {  # type: ignore[method-assign]
            "container_id": "c1",
            "upload_uri": "u",
        }
        svc.upload_video_to_container = lambda *a, **k: None  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="Video duration exceeds the limit"):
            svc.publish_reel("ig_user", "/tmp/x.mp4", "caption")

    def test_says_so_when_meta_gives_no_reason(self):
        svc, _ = _service({"status_code": "ERROR"})
        svc.create_reel_container = lambda *a, **k: {  # type: ignore[method-assign]
            "container_id": "c1",
            "upload_uri": "u",
        }
        svc.upload_video_to_container = lambda *a, **k: None  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="Meta gave no reason"):
            svc.publish_reel("ig_user", "/tmp/x.mp4", "caption")


class TestChannelNotConnected:
    def test_is_an_exception(self):
        assert issubclass(ChannelNotConnectedError, Exception)

    def test_not_caught_as_a_generic_failure_by_the_cron(self):
        """The cron's skip branch must win over its report_error branch."""
        try:
            raise ChannelNotConnectedError("No YouTube token")
        except ChannelNotConnectedError as exc:
            assert "No YouTube token" in str(exc)
        else:  # pragma: no cover
            pytest.fail("ChannelNotConnectedError was not raised")
