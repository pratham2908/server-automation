"""Rules for moving a scheduled YouTube video to a new time.

A scheduled upload is a **private** video carrying a ``publishAt``; YouTube
flips it public when that moment arrives. Rescheduling is therefore a change to
``status.publishAt`` on a video that is still private, which is all this module
decides. Pure, so the edge cases are testable without YouTube.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

# YouTube rejects a publishAt that is not in the future. A small margin keeps a time that is
# "now" by the clock from failing after a round trip, or by a second of skew between machines.
MIN_LEAD = timedelta(minutes=1)


def to_utc(moment: datetime) -> datetime:
    """Aware times are converted; naive ones are UTC, matching how Mongo hands datetimes back."""
    return moment.astimezone(UTC) if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def publish_at_for(new_time: datetime, now: datetime) -> str:
    """The RFC 3339 UTC string YouTube wants, or ``ValueError`` if the time cannot be used.

    A reschedule is always to a *future* time. Publishing immediately is a different action (it
    changes the video's visibility, not its schedule), so a past time is refused instead of being
    quietly treated as "now".
    """
    moment = to_utc(new_time)
    if moment < to_utc(now) + MIN_LEAD:
        raise ValueError(
            "Pick a time at least a minute from now. To publish right away, make the video public instead."
        )
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def check_reschedulable(status: dict[str, Any]) -> None:
    """``ValueError`` unless the video is still private, the only state a ``publishAt`` applies to."""
    privacy = status.get("privacyStatus")
    if privacy != "private":
        raise ValueError(
            f"This video is already {privacy or 'live'} on YouTube, so it cannot be rescheduled. "
            "A video can only be moved while it is still private and waiting to publish."
        )


def apply_publish_at(status: dict[str, Any], publish_at: str) -> dict[str, Any]:
    """A copy of ``status`` with the new time and nothing else changed.

    ``videos.update`` **replaces** the part it is sent, so the whole existing status is carried
    through: sending ``publishAt`` alone would reset ``embeddable``, ``license`` and
    ``selfDeclaredMadeForKids`` to their defaults.
    """
    updated = dict(status)
    updated["privacyStatus"] = "private"
    updated["publishAt"] = publish_at
    return updated


def error_reason(exc: Exception) -> str:
    """Google's own explanation for a refused call, trimmed to something a person can act on.

    ``HttpError.reason`` carries the API's message ("The scheduled publishing time must be in the
    future"); ``str(exc)`` is the whole URL and status line.
    """
    reason = getattr(exc, "reason", None)
    return str(reason or exc)[:300]
