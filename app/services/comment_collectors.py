"""Collectors: questions a channel asks of every comment, whose answers are counted into a list.

Pure: no I/O. A collector is written by the owner ("Which city does this commenter want us to map
next?"), not coded per niche. The worker (``comment_collector_worker``) reads each new comment once, asks
every collector that applies to its reel in one AI call (``build_prompt`` / ``parse_answers``), and the
store keeps one row per answer. The list is a count over those rows; a person's answer counts once per
reel however often they repeat it, and the likes on their comment are shown beside it.

Answers are cleaned here, not trusted as typed: a fixed-choice collector keeps only its choices, an open one
keeps the AI's usual name for the thing, and the owner's merges ("NYC" into "New York") are applied to
every answer from then on.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.timezone import assume_utc

MAX_COLLECTORS = 10
MAX_CHOICES = 30
# One comment's answers for one collector; a comment naming twelve cities is a list, not a vote.
MAX_ANSWERS = 5
ANSWER_MAX_LEN = 60

AnswerType = Literal["open", "choices"]
AppliesMode = Literal["all", "property", "reels"]


class AppliesTo(BaseModel):
    """Which reels a collector reads: all of them, those tagged with a property value, or picked ones."""

    mode: AppliesMode = "all"
    property_id: str | None = None
    value_ids: list[str] = Field(default_factory=list, max_length=50)
    media_ids: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _complete(self) -> AppliesTo:
        if self.mode == "property" and (not self.property_id or not self.value_ids):
            raise ValueError("pick a property and at least one of its values")
        if self.mode == "reels" and not self.media_ids:
            raise ValueError("pick at least one reel")
        return self


class CollectorIn(BaseModel):
    """What the owner sets. Everything else (id, merges, times) the server keeps."""

    name: str = Field(min_length=1, max_length=60)
    question: str = Field(min_length=3, max_length=300)
    guidance: str = Field(default="", max_length=1500)
    answer_type: AnswerType = "open"
    choices: list[str] = Field(default_factory=list, max_length=MAX_CHOICES)
    multiple: bool = False
    applies_to: AppliesTo = Field(default_factory=AppliesTo)
    enabled: bool = True

    @field_validator("name", "question", "guidance")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("choices")
    @classmethod
    def _clean_choices(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for v in values:
            label = clean_label(v)
            if label and fold(label) not in seen:
                seen.add(fold(label))
                out.append(label)
        return out

    @model_validator(mode="after")
    def _choices_when_needed(self) -> CollectorIn:
        if self.answer_type == "choices" and len(self.choices) < 2:
            raise ValueError("a fixed-choice collector needs at least two choices")
        return self


class Collector(CollectorIn):
    id: str
    channel_id: str
    # Folded alias -> the answer it counts as. Applied to every answer from the moment it is set.
    merges: dict[str, str] = Field(default_factory=dict)
    # Comments written before this are not read (unless copied in): a new question has no old answers.
    active_from: datetime
    created_at: datetime
    updated_at: datetime


def fold(text: str) -> str:
    """The comparison key for an answer: no accents, case, punctuation or extra spaces ("São Paulo" = "sao paulo")."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", plain.lower()).strip()


def clean_label(text: Any) -> str:
    """An answer as it is shown: trimmed, single-spaced, at most ``ANSWER_MAX_LEN`` characters."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip().strip("\"'")[:ANSWER_MAX_LEN].strip()


def applies(collector: Collector, media_id: str, properties: Mapping[str, Sequence[str]] | None) -> bool:
    """Whether *collector* reads comments on this reel (its property tags are ``{property_id: [value_id]}``)."""
    if not collector.enabled:
        return False
    rule = collector.applies_to
    if rule.mode == "all":
        return True
    if rule.mode == "reels":
        return media_id in rule.media_ids
    tagged = set((properties or {}).get(rule.property_id or "") or [])
    return bool(tagged & set(rule.value_ids))


def _key(index: int) -> str:
    return f"q{index + 1}"


def build_prompt(comment: str, collectors: Sequence[Collector], reel_title: str | None = None) -> str:
    """One prompt asking every collector's question of one comment, answered as one JSON object.

    The reel's title says what the comment is replying to: on "Where should 4 friends meet in Paris?" a
    "Châtelet" answers the reel, it doesn't ask for a city.
    """
    blocks = []
    for i, c in enumerate(collectors):
        lines = [f'"{_key(i)}": {c.question}']
        if c.guidance:
            lines.append(f"  Guidance: {c.guidance}")
        if c.answer_type == "choices":
            lines.append(f"  Answer only with these, spelled exactly: {json.dumps(c.choices, ensure_ascii=False)}.")
        else:
            lines.append('  Answer with the thing\'s usual English name, short ("New York", not "nyc pls").')
        lines.append("  Several answers allowed." if c.multiple else "  At most one answer: the first they give.")
        blocks.append("\n".join(lines))
    keys = ", ".join(f'"{_key(i)}"' for i in range(len(collectors)))
    title = reel_title.strip().split("\n")[0][:200] if reel_title else ""
    context = f"\nThe comment was written on a reel titled {json.dumps(title, ensure_ascii=False)}.\n" if title else ""
    return f"""Read one Instagram comment and answer each question about it.
{context}
{chr(10).join(blocks)}

Rules:
- Answer only from what the comment itself says. A comment that doesn't answer a question gets an empty list for it.
- Praise, emoji, jokes and replies to other people answer nothing unless they really do answer the question.
- Something mentioned in passing is not an answer: only count what the commenter is asking for or telling us.
- A reply to the reel's own question (its title) is not an answer to these questions unless it really answers them.

Comment: {json.dumps(comment, ensure_ascii=False)}

Return a JSON object with exactly the keys {keys}, each a list of strings. JSON only."""


def _canonical(collector: Collector, raw: str) -> str | None:
    label = clean_label(raw)
    if not label:
        return None
    merged = collector.merges.get(fold(label))
    if merged:
        return merged
    if collector.answer_type == "choices":
        by_fold = {fold(c): c for c in collector.choices}
        return by_fold.get(fold(label))
    return label


def answers_for(collector: Collector, raw: Any) -> list[str]:
    """One collector's cleaned answers from the model's list for it (merged, deduplicated, capped)."""
    items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        answer = _canonical(collector, item) if isinstance(item, str) else None
        if answer and fold(answer) not in seen:
            seen.add(fold(answer))
            out.append(answer)
    return out[: (MAX_ANSWERS if collector.multiple else 1)]


def parse_answers(reply: Mapping[str, Any], collectors: Sequence[Collector]) -> dict[str, list[str]]:
    """Each collector's answers, by collector id, from the model's JSON object (missing keys answer nothing)."""
    return {c.id: answers_for(c, reply.get(_key(i))) for i, c in enumerate(collectors)}


# Likes on a comment come mostly in its first day, so it is re-read often while young and rarely after.
# For ~150 answers a day this is ~230 Graph calls a day instead of ~960 at a flat 30 minutes.
LIKES_WINDOW = timedelta(days=14)
LIKES_SCHEDULE: tuple[tuple[timedelta, timedelta], ...] = (
    (timedelta(days=1), timedelta(minutes=30)),
    (timedelta(days=3), timedelta(hours=3)),
    (LIKES_WINDOW, timedelta(hours=24)),
)


def likes_due(commented_at: datetime, likes_at: datetime | None, now: datetime) -> bool:
    """Whether a comment's likes should be read again now."""
    now = assume_utc(now)
    age = now - assume_utc(commented_at)
    if age >= LIKES_WINDOW:
        return False
    if likes_at is None:
        return True
    every = next(gap for limit, gap in LIKES_SCHEDULE if age < limit)
    return now - assume_utc(likes_at) >= every


def tally(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The list: one entry per answer with how many people gave it, on how many comments, and their likes.

    A person counts once per reel for an answer (repeating "Tokyo" five times is one vote), and the likes
    are the sum over the comments counted, each comment once.
    """
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        answer = str(r.get("answer") or "")
        if not answer:
            continue
        g = groups.setdefault(
            fold(answer),
            {"answer": answer, "votes": set(), "people": set(), "comments": set(), "likes": 0, "last_at": None},
        )
        person = str(r.get("author") or "")
        g["votes"].add((person, str(r.get("media_id") or "")))
        g["people"].add(person)
        cid = str(r.get("comment_id") or "")
        if cid not in g["comments"]:
            g["comments"].add(cid)
            g["likes"] += int(r.get("like_count") or 0)
        at = r.get("commented_at")
        if isinstance(at, datetime) and (g["last_at"] is None or assume_utc(at) > g["last_at"]):
            g["last_at"] = assume_utc(at)
    out = [
        {
            "answer": g["answer"],
            "votes": len(g["votes"]),
            "people": len(g["people"]),
            "comments": len(g["comments"]),
            "likes": g["likes"],
            "last_at": g["last_at"],
        }
        for g in groups.values()
    ]
    out.sort(key=lambda e: (-e["votes"], -e["likes"], fold(e["answer"])))
    return out
