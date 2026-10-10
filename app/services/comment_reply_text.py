"""Text rules for comment replies: what the model is told about the video, and how its answer is cleaned.

Pure, so the prompt context and the "does this read as written by a person"
rules are testable without a model.
"""

from __future__ import annotations

import re

# Enough to tell the model what kind of video this is without paying for a wall of hashtags on
# every call: the description is sent once per batch of comments and once per reply.
MAX_DESCRIPTION_CHARS = 600

_SPACED_DASH = re.compile(r"\s+[—–-]\s+|\s*[—–]\s*")
_WHITESPACE = re.compile(r"[ \t]+")
_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’"}

_CHAR_SWAPS = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "…": "...",
    " ": " ",
}


def video_context(video: dict[str, object]) -> str:
    """The video's title and description as one block the model can read, or ``""`` if it has neither.

    The description is cut on a word boundary: a half-word at the end reads like a
    corrupted prompt and the model sometimes tries to complete it.
    """
    title = str(video.get("title") or "").strip()
    description = " ".join(str(video.get("description") or "").split())
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    parts = []
    if title:
        parts.append(f"Title: {title}")
    if description:
        parts.append(f"Description: {description}")
    return "\n".join(parts)


def humanise_reply(text: str) -> str:
    """Strip the typographic tells of machine-written text from a reply.

    Em and en dashes (and a hyphen standing in for one) are the loudest: people
    typing a comment on a phone use a comma, a full stop or nothing. Curly quotes
    and the single-character ellipsis are the next giveaways. Hyphens inside words
    (``step-by-step``) are left alone.
    """
    cleaned = text.strip()
    for old, new in _CHAR_SWAPS.items():
        cleaned = cleaned.replace(old, new)
    cleaned = _SPACED_DASH.sub(", ", cleaned)
    # A dash straight after sentence punctuation would leave ",," or ".,".
    cleaned = re.sub(r"([.,!?]),\s*", r"\1 ", cleaned)
    cleaned = re.sub(r",\s*([.!?])", r"\1", cleaned)
    # Models often wrap the whole reply in quotation marks.
    if len(cleaned) > 1 and _QUOTE_PAIRS.get(cleaned[0]) == cleaned[-1]:
        cleaned = cleaned[1:-1].strip()
    # A trailing dash leaves a dangling comma behind.
    return _WHITESPACE.sub(" ", cleaned).strip(" ,")
