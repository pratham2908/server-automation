"""Voted-city reels: the rules, with no I/O.

The audience votes for the next city by asking for it in the comments, and every like on such a comment is
a vote. The city with the most likes in total wins. Its reel goes up by hand as a reply-with-reel to the
top comment, which the Graph API can't do, and opens on the real like count. Once it is live, the server
posts the first comment and thanks a few more of the people who asked.

Honesty rule (owner): only claim a vote that really happened, with the real like count. So the caption's
first line is built here from a live count and never taken from the caller.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from app.services import comment_collectors as cc
from app.services.instagram import graph_error_code
from app.timezone import assume_utc

CAPTION_PREFIX = "You voted."
FIRST_COMMENT = "Comment your city 👇 The most-liked city is our next video."
# Rotated so three thank-yous in a row don't read as a bot.
REPLY_TEMPLATES = (
    "{city} is up. You picked it 🙌",
    "Done! {city} is live now 📍",
    "{city} is up. You picked it 🔥",
)
REPLIES = 3
# Kept on the kit for the follow-up to choose from: the top requesters, a few spare for deleted comments.
CANDIDATES = 10
CAPTION_MAX = 2200
# A post is matched to a kit only if it went up after the kit was made (with a little clock slack).
MATCH_SLACK = timedelta(minutes=10)
GONE_CODE = 100

_SHORTCODE = re.compile(r"instagram\.com/(?:reels?|p|tv)/([A-Za-z0-9_-]+)")


def shortcode(permalink: str | None) -> str | None:
    found = _SHORTCODE.search(permalink or "")
    return found.group(1) if found else None


def deep_link(code: str | None, comment_id: str) -> str | None:
    """The link that opens the reel scrolled to this comment, for replying to it with a reel."""
    return f"https://www.instagram.com/reel/{code}/c/{comment_id}/" if code else None


def first_line(city: str, likes: int) -> str:
    return f"{CAPTION_PREFIX} {city} won 📍 {likes:,} {'like' if likes == 1 else 'likes'}"


def compose_caption(city: str, likes: int, body: str | None) -> str:
    """The server's first line (the claim) over the caller's city-specific body.

    Raises ``ValueError`` for a body that makes its own "You voted." claim, or a caption over Instagram's limit.
    """
    head = first_line(city, likes)
    text = (body or "").strip()
    if text.casefold().startswith(CAPTION_PREFIX.casefold()):
        raise ValueError("the caption body can't start with 'You voted.': the server writes that line")
    caption = f"{head}\n\n{text}" if text else head
    if len(caption) > CAPTION_MAX:
        raise ValueError(f"the caption is {len(caption)} characters; Instagram allows {CAPTION_MAX}")
    return caption


def reply_text(index: int, city: str) -> str:
    return REPLY_TEMPLATES[index % len(REPLY_TEMPLATES)].format(city=city)


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def caption_head(caption: str | None) -> str:
    """The caption's first non-empty line, spaces collapsed."""
    for line in (caption or "").splitlines():
        if line.strip():
            return _norm(line)
    return ""


def media_time(item: Mapping[str, Any]) -> datetime | None:
    raw = item.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


def matches_kit(kit: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    """Is this post the kit's reel? Its caption opens with the kit's first line, or with "You voted." and the
    kit's city; and it went up after the kit was made."""
    head = caption_head(item.get("caption"))
    if not head.startswith(_norm(CAPTION_PREFIX)):
        return False
    kit_head = caption_head(kit.get("caption"))
    if head != kit_head and cc.fold(str(kit.get("city") or "")) not in cc.fold(head):
        return False
    posted = media_time(item)
    made = kit.get("created_at")
    if posted is not None and isinstance(made, datetime) and posted < assume_utc(made) - MATCH_SLACK:
        return False
    return True


def city_key(answer: str, merges: Mapping[str, str]) -> tuple[str, str]:
    """(key, label) for an answer, through the collector's merges."""
    key = cc.fold(answer)
    label = merges.get(key) or answer
    return cc.fold(label), label


def _comment_out(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "comment_id": str(row["comment_id"]),
        "media_id": str(row.get("media_id") or ""),
        "username": str(row.get("username") or ""),
        "author": str(row.get("author") or ""),
        "text": str(row.get("text") or ""),
        "likes": int(row.get("like_count") or 0),
        "commented_at": row.get("commented_at"),
    }


def city_groups(
    rows: Iterable[Mapping[str, Any]],
    merges: Mapping[str, str],
    done: Iterable[str] = (),
    own_usernames: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """The vote: one entry per city, most likes first. Pure.

    A comment counts once per city, with its likes. Deleted comments, our own comments and cities already
    posted (*done*, as folded keys) are left out. A city's comments are ranked by likes, then oldest first,
    so the first person to ask wins a tie.
    """
    done_keys = {cc.fold(d) for d in done}
    own = {u.casefold().lstrip("@") for u in own_usernames if u}
    cities: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("comment_gone") or str(row.get("username") or "").casefold() in own:
            continue
        key, label = city_key(str(row.get("answer") or ""), merges)
        if not key or key in done_keys or (key, str(row["comment_id"])) in seen:
            continue
        seen.add((key, str(row["comment_id"])))
        group = cities.setdefault(key, {"key": key, "labels": {}, "comments": []})
        group["labels"][label] = group["labels"].get(label, 0) + 1
        group["comments"].append(_comment_out(row))
    out = []
    for group in cities.values():
        comments = sorted(group["comments"], key=lambda c: _when(c["commented_at"]))
        comments.sort(key=lambda c: c["likes"], reverse=True)
        labels: dict[str, int] = group["labels"]
        out.append(
            {
                "city": max(labels, key=lambda name: labels[name]),
                "key": group["key"],
                "likes": sum(c["likes"] for c in comments),
                "comments": len(comments),
                "people": len({c["author"] or c["username"] for c in comments}),
                "top": comments[0],
                "others": comments[1:],
            }
        )
    out.sort(key=lambda g: (g["likes"], g["comments"], g["people"]), reverse=True)
    return out


def _when(value: Any) -> datetime:
    return assume_utc(value) if isinstance(value, datetime) else datetime.max.replace(tzinfo=timezone.utc)


def pick_repliers(
    candidates: Sequence[Mapping[str, Any]], top: Mapping[str, Any], gone: Iterable[str] = (), n: int = REPLIES
) -> list[dict[str, Any]]:
    """The next *n* requesters to thank: distinct people, not the top comment's author, comment still up."""
    gone_ids = set(gone)
    taken = {str(top.get("author") or top.get("username") or "")}
    out: list[dict[str, Any]] = []
    for c in candidates:
        who = str(c.get("author") or c.get("username") or "")
        if str(c["comment_id"]) in gone_ids or who in taken or c["comment_id"] == top.get("comment_id"):
            continue
        taken.add(who)
        out.append(dict(c))
        if len(out) == n:
            break
    return out


def refused(exc: Exception) -> bool:
    """Did Instagram clearly refuse the post (so nothing was posted), rather than leave us unsure?

    A 4xx with a Graph error body is a refusal. A timeout, a dropped connection or a 5xx may have posted,
    so it is checked before any retry.
    """
    if not isinstance(exc, requests.HTTPError) or exc.response is None:
        return False
    return 400 <= exc.response.status_code < 500 and graph_error_code(exc) is not None


def target_gone(exc: Exception) -> bool:
    return refused(exc) and graph_error_code(exc) == GONE_CODE
