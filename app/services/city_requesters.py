"""Telling the people who asked for a city that its meet reel is out: the rules, with no I/O.

When a "Where Should 4 Friends Meet? #N: <City>" reel goes up on @themapworld, everyone who asked for
that city in our comments gets one public reply under their comment (owner OK, 2026-11-11). The texts
are fixed and owner-approved, rotated so a run of them doesn't read as a bot, and carry no links.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from app.services import comment_collectors as cc

TEMPLATES = (
    "{city} is live on our profile now 🗺️ go find your friends' spot!",
    "You asked, we made it 📍 {city} just dropped, check our profile",
    "{city}'s up! Tell us if they picked the right meeting spot 👀",
)

# "Where Should 4 Friends Meet? #8: Istanbul, Across Two Continents 📍" -> "Istanbul"
_HEADLINE = re.compile(r"^\s*where should 4 friends meet\?\s*#\s*\d+\s*:\s*(.+)$", re.IGNORECASE)
_TAIL = re.compile(r"[\s\W_]+$")


def headline_city(caption: str | None) -> str | None:
    """The city a meet reel's caption names in its first line, or None for any other reel."""
    first = next((line for line in (caption or "").splitlines() if line.strip()), "")
    found = _HEADLINE.match(first)
    if not found:
        return None
    city = _TAIL.sub("", found.group(1).split(",")[0]).strip()
    return city or None


def reply_text(index: int, city: str) -> str:
    return TEMPLATES[index % len(TEMPLATES)].format(city=city)


def mentions(text: str | None, keys: Iterable[str]) -> bool:
    """Does the comment name the city, by its name or an alias, as whole words?"""
    folded = f" {cc.fold(text or '')} "
    return any(key and f" {key} " in folded for key in keys)


def _when(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def one_per_person(rows: Iterable[Mapping[str, Any]], own: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Each person's comment to reply under: their most liked ask, the newest of equals. Oldest asker first.

    Our own comments and comments with no username are left out.
    """
    skip = {u.lower().lstrip("@") for u in own if u}
    best: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        who = str(row.get("username") or "").lower().lstrip("@")
        if not who or who in skip:
            continue
        held = best.get(who)
        rank = (int(row.get("like_count") or 0), _when(row.get("commented_at")))
        if held is None or rank > (int(held.get("like_count") or 0), _when(held.get("commented_at"))):
            best[who] = row
    picked = [{**row, "username": who} for who, row in best.items()]
    return sorted(picked, key=lambda r: _when(r.get("commented_at")))


def already_told(replies: Iterable[Mapping[str, Any]], own: str, keys: Iterable[str]) -> str | None:
    """The id of our reply in this thread that names the city, if we already told them (by hand or not)."""
    keys = list(keys)
    for reply in replies:
        if str(reply.get("author") or "").lower() == own.lower() and mentions(str(reply.get("text") or ""), keys):
            return str(reply.get("comment_id") or "") or "unknown"
    return None
