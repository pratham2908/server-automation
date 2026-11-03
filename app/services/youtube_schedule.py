"""Rules for changing a scheduled YouTube video: its time, and the title, description and tags it will go live with.

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


# What YouTube lets a title, description and tag list be. A request over a limit is refused by us with
# a plain reason instead of coming back from Google as a bare 400 after the other change already landed.
MAX_TITLE = 100
MAX_DESCRIPTION_BYTES = 5000
MAX_TAGS_CHARS = 500

# The parts of a snippet that can be written back. ``videos.update`` replaces the snippet, so these are
# carried through unchanged; the rest of what ``videos.list`` returns (thumbnails, publishedAt, channel) is
# read-only and is left out rather than echoed to an endpoint that does not accept it.
_WRITABLE_SNIPPET = ("title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage")


def merged_tags(existing: list[str] | None, new: list[str], mode: str | None) -> list[str]:
    """The tag list after an edit: ``append`` adds to the existing ones, anything else replaces them.

    Order is kept and duplicates dropped (YouTube shows tags in the order given).
    """
    combined = [*(existing or []), *new] if mode == "append" else list(new)
    return list(dict.fromkeys(combined))


def check_snippet_limits(title: str | None, description: str | None, tags: list[str] | None) -> None:
    """``ValueError`` if a title, description or tag list is one YouTube would refuse."""
    if title is not None:
        if not title.strip():
            raise ValueError("The title cannot be empty")
        if len(title) > MAX_TITLE:
            raise ValueError(f"A YouTube title is at most {MAX_TITLE} characters; this one is {len(title)}")
    if description is not None and len(description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        raise ValueError(f"A YouTube description is at most {MAX_DESCRIPTION_BYTES} bytes")
    if tags is not None and sum(len(tag) for tag in tags) > MAX_TAGS_CHARS:
        raise ValueError(f"YouTube tags add up to at most {MAX_TAGS_CHARS} characters")


def apply_snippet_changes(
    snippet: dict[str, Any], *, title: str | None = None, description: str | None = None, tags: list[str] | None = None
) -> dict[str, Any]:
    """The writable part of ``snippet`` with the given fields changed and everything else kept."""
    updated = {key: snippet[key] for key in _WRITABLE_SNIPPET if key in snippet}
    if title is not None:
        updated["title"] = title
    if description is not None:
        updated["description"] = description
    if tags is not None:
        updated["tags"] = tags
    return updated
