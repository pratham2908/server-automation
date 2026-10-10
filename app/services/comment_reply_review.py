"""Rules for the comment-reply review queue.

A channel in ``review`` mode drafts a reply for each comment and then waits for a
person to approve it, instead of posting on its own. Everything here is pure so
the cycle, the router and the tests share one definition of "what is allowed".
"""

from __future__ import annotations

from typing import Any

from app.timezone import now_ist

MODE_AUTO = "auto"
MODE_REVIEW = "review"
MODES = (MODE_AUTO, MODE_REVIEW)
DEFAULT_MODE = MODE_AUTO  # existing channels keep replying on their own until switched

STATUS_PENDING = "pending_approval"
STATUS_POSTING = "posting"  # claimed by an approve call; stops a double-click posting twice
STATUS_REPLIED = "replied"
STATUS_REJECTED = "rejected"

# Spam is never worth a person's attention; every other sentiment is drafted for review.
REVIEWABLE_SENTIMENTS = ("positive", "negative", "neutral")

# Same platform ceilings as the first-comment feature: exceeding them is a hard API error.
MAX_REPLY_LENGTH: dict[str, int] = {"instagram": 2200, "youtube": 10000}


def normalise_mode(value: str | None) -> str:
    """Unknown or missing values fall back to ``auto`` so old channel docs keep working."""
    return value if value in MODES else DEFAULT_MODE


def is_reviewable(sentiment: str) -> bool:
    return sentiment in REVIEWABLE_SENTIMENTS


def own_identities(channel: dict[str, Any]) -> set[str]:
    """Every lowercase name this channel's own comments could be signed with.

    Instagram reports a username, which the channel doc stores in
    ``instagram_username`` or as ``handle``; ``name`` is the display name and only
    sometimes equals it, so it is a last resort rather than the only key.
    """
    names = {
        channel.get("instagram_username"),
        channel.get("handle"),
        channel.get("name"),
    }
    return {str(n).lstrip("@").strip().lower() for n in names if n and str(n).strip()}


def is_own_comment(author: str, identities: set[str]) -> bool:
    """True only for a known author. An empty author is *unknown*, not "someone else"."""
    return bool(author) and author.lstrip("@").strip().lower() in identities


def validate_reply(text: str | None, platform: str) -> str:
    """The reply as it will be posted, or ``ValueError`` saying what to fix."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("A reply cannot be empty")
    limit = MAX_REPLY_LENGTH.get(platform, MAX_REPLY_LENGTH["youtube"])
    if len(cleaned) > limit:
        raise ValueError(f"A {platform} reply can be at most {limit} characters; this one is {len(cleaned)}")
    return cleaned


def build_pending_row(
    *,
    channel_id: str,
    platform: str,
    video: dict[str, Any],
    comment: dict[str, Any],
    sentiment: str,
    suggested_reply: str,
) -> dict[str, Any]:
    """The ``comment_replies`` document for a drafted-but-unsent reply."""
    return {
        "channel_id": channel_id,
        "video_id": video.get("video_id", ""),
        "video_title": video.get("title", ""),
        "video_url": comment.get("video_url", ""),
        "platform": platform,
        "comment_id": comment["comment_id"],
        "comment_text": comment.get("text", ""),
        "comment_author": comment.get("author", ""),
        "comment_url": comment.get("comment_url", ""),
        "comment_published_at": comment.get("published_at", ""),
        "sentiment": sentiment,
        "status": STATUS_PENDING,
        "suggested_reply": suggested_reply,
        "reply_text": suggested_reply,
        "replied_at": now_ist(),  # the field the history list sorts by; reused as "drafted at"
    }
