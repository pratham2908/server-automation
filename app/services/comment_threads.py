"""Comment threads as the inbox shows them.

Both platforms are two levels deep: a viewer's top-level comment, and a flat list
of replies under it (Instagram files a reply-to-a-reply under the same parent, and
so does YouTube). Everything here is pure, so the thread shape, who-spoke-last and
the transcript handed to the model are testable without either API.
"""

from __future__ import annotations

from typing import Any

from app.services.comment_reply_review import is_own_comment

PLATFORM_YOUTUBE = "youtube"
PLATFORM_INSTAGRAM = "instagram"

# Enough for the model to see how a conversation went without paying for a thread that has run to
# hundreds of replies. The newest are kept: they are what is being answered.
MAX_TRANSCRIPT_MESSAGES = 30


def make_message(
    platform: str,
    raw: dict[str, Any],
    *,
    own_identities: set[str],
    own_youtube_channel_id: str = "",
) -> dict[str, Any]:
    """One comment or reply in the inbox's shape, with ``is_own`` worked out.

    YouTube identifies an author by channel id, which is exact; Instagram only
    has a username, so it is matched against every name the channel goes by. An
    unknown author is never "own": that would hide a viewer's comment from the
    ``needs_reply`` filter.
    """
    if platform == PLATFORM_YOUTUBE:
        author_id = str(raw.get("author_channel_id") or "")
        is_own = bool(own_youtube_channel_id) and author_id == own_youtube_channel_id
    else:
        is_own = is_own_comment(str(raw.get("author") or ""), own_identities)
    return {
        "comment_id": str(raw.get("comment_id") or ""),
        "author": str(raw.get("author") or ""),
        "text": str(raw.get("text") or ""),
        "published_at": str(raw.get("published_at") or ""),
        "like_count": int(raw.get("like_count") or 0),
        "avatar_url": raw.get("avatar_url") or None,
        "is_own": is_own,
    }


def build_thread(
    top: dict[str, Any],
    replies: list[dict[str, Any]],
    *,
    video_id: str,
    platform: str,
    comment_url: str = "",
) -> dict[str, Any]:
    """A thread: the viewer's comment, then replies oldest first (the order they were read in)."""
    ordered = sorted(replies, key=lambda r: r["published_at"])
    last = ordered[-1] if ordered else top
    return {
        "thread_id": top["comment_id"],
        "video_id": video_id,
        "platform": platform,
        "message": top,
        "replies": ordered,
        "reply_count": len(ordered),
        "last_activity": last["published_at"] or top["published_at"],
        # Whoever spoke last decides: a thread we answered and the viewer then answered again is waiting
        # on us, and so is a fresh top-level comment nobody has answered.
        "needs_reply": not last["is_own"],
        "comment_url": comment_url,
        "pending_draft": None,
    }


def sort_threads(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Most recently active first, so a new follow-up on an old thread surfaces."""
    return sorted(threads, key=lambda t: t["last_activity"], reverse=True)


def all_messages(thread: dict[str, Any]) -> list[dict[str, Any]]:
    return [thread["message"], *thread["replies"]]


def find_message(thread: dict[str, Any], comment_id: str) -> dict[str, Any] | None:
    return next((m for m in all_messages(thread) if m["comment_id"] == comment_id), None)


def default_target(thread: dict[str, Any]) -> dict[str, Any]:
    """The message a draft answers when none is chosen: the newest one from a viewer.

    If the last word is ours there is nothing new to answer, so it falls back to the newest viewer
    message anyway rather than refusing.
    """
    for message in reversed(all_messages(thread)):
        if not message["is_own"]:
            return message
    top: dict[str, Any] = thread["message"]
    return top


def thread_transcript(thread: dict[str, Any], target_comment_id: str | None = None) -> str:
    """The conversation as plain lines for the model, the message being answered marked.

    Our own messages are labelled ``You`` and viewers' by their name, so the model can see which
    side said what, and does not repeat something already said earlier in the thread.
    """
    messages = all_messages(thread)
    dropped = max(0, len(messages) - MAX_TRANSCRIPT_MESSAGES)
    if dropped:
        # Always keep the opening comment: it is what the whole thread is about.
        messages = [messages[0], *messages[-(MAX_TRANSCRIPT_MESSAGES - 1) :]]
    target = target_comment_id or default_target(thread)["comment_id"]
    lines: list[str] = []
    for index, message in enumerate(messages):
        speaker = "You (the channel)" if message["is_own"] else (message["author"] or "A viewer")
        text = " ".join(message["text"].split())
        marker = "   <-- reply to this" if message["comment_id"] == target else ""
        lines.append(f"{speaker}: {text}{marker}")
        if dropped and index == 0:
            lines.append(f"[{dropped} earlier messages omitted]")
    return "\n".join(lines)


def with_mention(text: str, target: dict[str, Any], thread: dict[str, Any]) -> str:
    """Prefix an ``@mention`` when answering a nested reply.

    Both platforms flatten the thread, so a reply to a reply lands under the parent with nothing to
    show who it is for. The mention is what makes it read as an answer. Not added when answering the
    opening comment (the parent already says it), when it is our own message, or when the text
    already starts with a mention.
    """
    is_top_level = target["comment_id"] == thread["thread_id"]
    if is_top_level or target["is_own"] or not target["author"]:
        return text
    stripped = text.lstrip()
    if stripped.startswith("@"):
        return text
    return f"@{target['author'].lstrip('@')} {stripped}"
