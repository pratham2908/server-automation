"""Translating a comment into English for the owner to read.

Pure prompt and reply handling; the model call and the cache sit in ``app.routers.comment_translate``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MAX_TEXT = 2200


def build_prompt(text: str) -> str:
    return (
        "Translate this Instagram comment into plain English for the account owner. Hindi or other Indian languages "
        "may be written in Latin letters (Hinglish); translate those too. Keep the tone, slang and emoji.\n"
        'Reply with JSON only: {"language": "<the language it is written in, e.g. Hindi, Hinglish, Spanish, '
        'English>", "english": "<the English translation; the comment unchanged if it already is English>"}\n\n'
        f"Comment:\n{text[:MAX_TEXT]}"
    )


def parse_translation(reply: Mapping[str, Any], original: str) -> dict[str, Any]:
    """``{"language", "english", "already_english"}`` from the model's reply; raises if it gave no translation."""
    english = reply.get("english")
    if not isinstance(english, str) or not english.strip():
        raise ValueError("the reply has no translation")
    language = reply.get("language")
    language = language.strip() if isinstance(language, str) and language.strip() else "Unknown"
    same = english.strip().casefold() == original.strip().casefold()
    return {
        "language": language,
        "english": english.strip(),
        "already_english": same or language.casefold() == "english",
    }
