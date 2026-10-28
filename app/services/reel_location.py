"""A reel's location tag: a Facebook place ID Instagram can attach, plus a name for the owner's eyes.

Instagram tags a location only by the place's Facebook page ID (the same rule as image posts), and it
validates the ID itself. The name is free text shown in the app; without an ID it is never sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MAX_PLACE_ID_LENGTH = 32
MAX_NAME_LENGTH = 120


def validate_place_id(value: str | None) -> str | None:
    """A cleaned place ID, ``None`` for blank, or ``ValueError`` if it isn't a number."""
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    if not cleaned.isdigit() or len(cleaned) > MAX_PLACE_ID_LENGTH:
        raise ValueError("Location place ID must be a number (a Facebook place ID)")
    return cleaned


def location_fields(location_id: str | None, location_name: str | None, platform: str) -> dict[str, Any]:
    """The video fields to write for a schedule request, or ``{}`` if it says nothing.

    ``None`` leaves a field as it is and an empty string clears it, so a reschedule that omits
    the location can't wipe one set earlier. Only Instagram reels take a location.
    """
    fields: dict[str, Any] = {}
    if location_id is not None:
        fields["location_id"] = validate_place_id(location_id)
    if location_name is not None:
        name = location_name.strip()
        if len(name) > MAX_NAME_LENGTH:
            raise ValueError(f"Location name is longer than {MAX_NAME_LENGTH} characters")
        fields["location_name"] = name or None
    if platform != "instagram" and any(value for value in fields.values()):
        raise ValueError("A location can only be set on an Instagram reel")
    return fields


def api_location_id(video: Mapping[str, Any]) -> str | None:
    """The place ID to send with the container, or ``None`` when there is none or it isn't usable."""
    value = video.get("location_id")
    return value if isinstance(value, str) and value.isdigit() else None


def is_location_refusal(message: str) -> bool:
    """Whether Instagram refused the container because of the location rather than anything else."""
    return "location" in message.casefold()
