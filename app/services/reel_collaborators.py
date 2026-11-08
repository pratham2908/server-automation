"""Collaborators on an Instagram reel or feed post: the accounts invited to co-author it.

Meta takes them as ``collaborators`` on the media container, a list of up to three usernames. Each one
gets an invite in the Instagram app and only shows as a co-author once they accept it; a username
Instagram can't invite (no such account, a private one, one that blocks invites) refuses the whole
container, so the reel is then published without them (see ``auto_publisher``) and the reason kept.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

# Meta's limit per post.
MAX_COLLABORATORS = 3
_USERNAME = re.compile(r"^[a-z0-9._]{1,30}$")


def clean_collaborators(value: list[str] | None) -> list[str] | None:
    """Usernames as Instagram spells them (no "@", lower case, each once), ``None`` for "say nothing".

    An empty list stays empty: it is how a caller clears collaborators set earlier.
    """
    if value is None:
        return None
    cleaned: list[str] = []
    for raw in value:
        name = raw.strip().removeprefix("@").lower()
        if not name:
            continue
        if not _USERNAME.fullmatch(name):
            raise ValueError(f"'{raw}' is not an Instagram username (letters, numbers, '.' and '_', up to 30)")
        if name not in cleaned:
            cleaned.append(name)
    if len(cleaned) > MAX_COLLABORATORS:
        raise ValueError(f"Instagram allows at most {MAX_COLLABORATORS} collaborators on a post")
    return cleaned


def collaborator_fields(collaborators: list[str] | None, platform: str, own_username: str = "") -> dict[str, Any]:
    """The video fields to write for a schedule or edit request, or ``{}`` if it says nothing.

    ``None`` leaves collaborators as they are and ``[]`` clears them, so a reschedule that omits the
    field can't wipe ones set earlier. Only Instagram takes collaborators.
    """
    cleaned = clean_collaborators(collaborators)
    if cleaned is None:
        return {}
    if cleaned and platform != "instagram":
        raise ValueError("Collaborators can only be set on an Instagram post")
    if own_username and own_username.lower() in cleaned:
        raise ValueError("A post can't invite its own account as a collaborator")
    return {"collaborators": cleaned}


def api_collaborators(doc: Mapping[str, Any]) -> list[str] | None:
    """The usernames to send with the container, or ``None`` when there are none."""
    value = doc.get("collaborators")
    if not isinstance(value, list):
        return None
    names = [str(v) for v in value if isinstance(v, str) and v]
    return names or None


def collaborators_param(collaborators: list[str] | None) -> str | None:
    """The ``collaborators`` form field Meta expects: a JSON array of usernames."""
    return json.dumps(collaborators) if collaborators else None


def is_collaborator_refusal(message: str) -> bool:
    """Whether Instagram refused the container because of a collaborator rather than anything else."""
    folded = message.casefold()
    return "collaborat" in folded or "coauthor" in folded or "co-author" in folded
