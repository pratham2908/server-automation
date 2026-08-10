"""The ``last_failure`` marker written onto a video when scheduling gives up.

A video can sit in ``ready`` for two very different reasons: it was just
produced, or a scheduled upload/publish failed every retry and the worker
bounced it back. Both leave ``status == "ready"``; this marker is what tells
them apart. Its presence means "needs attention", its absence means "fresh".

Kept deliberately small and dependency-free so it is trivial to unit test.
"""

from __future__ import annotations

from typing import Any

from app.timezone import now_ist

# Longest raw exception text we keep. Enough to debug with, short enough that a
# stringified traceback can never bloat the video document.
_MAX_DETAIL_CHARS = 500

# Ordered most-specific first; the first matching group wins. Each entry is
# (substrings, reason). Matching is case-insensitive on ``str(exc)``.
_REASON_RULES: list[tuple[tuple[str, ...], str]] = [
    (
        ("invalid_grant", "token has been expired", "invalid credentials", "unauthorized", "401"),
        "Channel authentication expired — reconnect the channel",
    ),
    (
        ("quotaexceeded", "ratelimitexceeded", "quota", "rate limit", "429"),
        "Platform quota or rate limit reached",
    ),
    (
        ("timed out", "timeout", "connection reset", "connection aborted", "connection error", "network"),
        "Network error reaching the platform",
    ),
]

_GENERIC_REASON = "Upload or publish failed"


def classify_failure_reason(exc: BaseException) -> str:
    """Map an exception to a short, user-facing reason string.

    Falls back to a generic reason for anything unrecognised — the raw text is
    preserved separately in the marker's ``detail`` field.
    """
    text = str(exc).lower()
    # The exception *type* name catches cases where the message is empty, e.g. a
    # bare ``RefreshError()`` still reads as an auth failure.
    text = f"{type(exc).__name__.lower()} {text}"
    for needles, reason in _REASON_RULES:
        if any(needle in text for needle in needles):
            return reason
    return _GENERIC_REASON


def build_failure_marker(*, stage: str, platform: str, attempts: int, exc: BaseException) -> dict[str, Any]:
    """Build the ``last_failure`` sub-document persisted on the video.

    ``stage`` is ``"upload"`` (YouTube) or ``"publish"`` (Instagram); ``platform``
    is passed explicitly so the two are never inferred from each other.
    """
    return {
        "stage": stage,
        "platform": platform,
        "reason": classify_failure_reason(exc),
        "detail": str(exc)[:_MAX_DETAIL_CHARS],
        "attempts": attempts,
        "failed_at": now_ist(),
    }
