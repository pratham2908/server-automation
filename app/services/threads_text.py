"""What a Threads post says, built from a video's packaging.

Pure: no I/O.

Threads caps a post at 500 characters and honours one topic tag, sent as its own
``topic_tag`` parameter rather than as ``#tag`` in the text (only the first
in-text tag would count, and it would eat into the 500). So the post is the
caption alone, and the first usable tag becomes the topic.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

MAX_TEXT_CHARS = 500
MAX_TOPIC_CHARS = 50

# Characters Threads refuses in a topic tag.
_TOPIC_FORBIDDEN = re.compile(r"[.&#]")


def _fits(text: str) -> str:
    """Cut to the limit at a word boundary, with an ellipsis, rather than mid-word."""
    if len(text) <= MAX_TEXT_CHARS:
        return text
    cut = text[: MAX_TEXT_CHARS - 1]
    space = cut.rfind(" ")
    if space > MAX_TEXT_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


def threads_text(video_doc: Mapping[str, Any]) -> str:
    """The post text: the packaged caption, else the title, within 500 characters."""
    body = str(video_doc.get("description") or "").strip() or str(video_doc.get("title") or "").strip()
    return _fits(body)


def topic_tag(video_doc: Mapping[str, Any]) -> str | None:
    """The first tag Threads would accept as a topic, or None."""
    for raw in video_doc.get("tags") or []:
        tag = " ".join(_TOPIC_FORBIDDEN.sub("", str(raw)).split())
        if tag:
            return tag[:MAX_TOPIC_CHARS].strip()
    return None
