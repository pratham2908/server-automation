"""The failure marker written onto a video when a scheduled upload/publish gives up."""

from app.services.publish_failure import build_failure_marker, classify_failure_reason


class RefreshError(Exception):
    """Stand-in shaped like google.auth.exceptions.RefreshError."""


# ------------------------------------------------------------------
# classify_failure_reason — exception -> short human string
# ------------------------------------------------------------------


def test_expired_credentials_are_reported_as_an_auth_problem():
    reason = classify_failure_reason(RefreshError("invalid_grant: Token has been expired or revoked."))
    assert reason == "Channel authentication expired — reconnect the channel"


def test_invalid_grant_message_on_a_plain_exception_is_still_auth():
    assert classify_failure_reason(Exception("400 invalid_grant")) == (
        "Channel authentication expired — reconnect the channel"
    )


def test_quota_and_rate_limit_map_to_one_reason():
    assert classify_failure_reason(Exception("quotaExceeded: daily limit")) == "Platform quota or rate limit reached"
    assert classify_failure_reason(Exception("rateLimitExceeded")) == "Platform quota or rate limit reached"
    assert classify_failure_reason(Exception("HTTP 429 Too Many Requests")) == "Platform quota or rate limit reached"


def test_timeouts_and_connection_errors_map_to_network():
    assert classify_failure_reason(TimeoutError("timed out")) == "Network error reaching the platform"
    assert classify_failure_reason(ConnectionError("connection reset")) == "Network error reaching the platform"


def test_unrecognised_error_falls_back_to_a_generic_reason():
    assert classify_failure_reason(Exception("something weird happened")) == "Upload or publish failed"


def test_classification_is_case_insensitive():
    assert classify_failure_reason(Exception("INVALID_GRANT")) == (
        "Channel authentication expired — reconnect the channel"
    )


# ------------------------------------------------------------------
# build_failure_marker — the sub-document persisted on the video
# ------------------------------------------------------------------


def test_marker_has_every_field_the_frontend_reads():
    marker = build_failure_marker(
        stage="publish",
        platform="instagram",
        attempts=5,
        exc=Exception("quotaExceeded"),
    )
    assert marker["stage"] == "publish"
    assert marker["platform"] == "instagram"
    assert marker["attempts"] == 5
    assert marker["reason"] == "Platform quota or rate limit reached"
    assert marker["detail"]  # raw text preserved for debugging
    assert marker["failed_at"].tzinfo is not None


def test_marker_detail_is_truncated_so_a_huge_traceback_string_cannot_bloat_the_doc():
    marker = build_failure_marker(
        stage="upload",
        platform="youtube",
        attempts=5,
        exc=Exception("x" * 5000),
    )
    assert len(marker["detail"]) <= 500


def test_marker_detail_survives_a_blank_exception_message():
    marker = build_failure_marker(stage="upload", platform="youtube", attempts=5, exc=Exception())
    assert isinstance(marker["detail"], str)
    assert marker["reason"] == "Upload or publish failed"
