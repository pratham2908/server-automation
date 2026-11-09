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
    """The comparison key for an answer: no accents, case, punctuation or extra spaces ("São Paulo" = "sao paulo").

    Other scripts are kept as they are ("香港" stays "香港"), so an answer in them still has a key to merge by.
    """
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return re.sub(r"[\W_]+", " ", plain.casefold()).strip()


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


# What kind of comment it is: one per comment, for the mix per reel and the most liked of each kind.
KINDS: tuple[tuple[str, str, str], ...] = (
    ("appreciation", "Appreciation", 'praise, love, hype ("this is fire", "love it", "🔥🔥")'),
    ("humour", "Humour", 'jokes, memes, teasing, laughing ("😭😭", "bro really said")'),
    ("request", "Request", 'asking for content: a topic, place, part two ("do Tokyo next", "make one on Dijkstra")'),
    ("question", "Question", 'asking something about the video or how it was made ("what app is this?")'),
    ("tag", "Tagging a friend", 'mainly @-mentioning someone to show them ("@sam look", "@ana this is us")'),
    ("personal", "Personal", 'their own story, opinion or where they\'re from ("I live there", "my commute is worse")'),
    ("criticism", "Criticism", 'saying it is wrong, correcting it, or disliking it ("that\'s not the shortest path")'),
    ("spam", "Spam", "self-promotion, bots, links, giveaways"),
    ("other", "Other", "emoji alone, unclear, or none of these"),
)
KIND_IDS = tuple(k for k, _, _ in KINDS)
KIND_LABELS = {k: label for k, label, _ in KINDS}
CAPTION_MAX = 400


def _context(reel_title: str | None, caption: str | None) -> str:
    title = (reel_title or "").strip().split("\n")[0][:200]
    about = re.sub(r"\s+", " ", caption or "").strip()[:CAPTION_MAX]
    if about and title and about.startswith(title):
        about = about[len(title) :].lstrip(" .,:;|-–—").strip()
    lines = []
    if title:
        lines.append(f"The comment was written on a reel titled {json.dumps(title, ensure_ascii=False)}.")
    if about:
        lines.append(f"The reel's caption: {json.dumps(about, ensure_ascii=False)}")
    return ("\n" + "\n".join(lines) + "\n") if lines else ""


def build_prompt(
    comment: str,
    collectors: Sequence[Collector],
    reel_title: str | None = None,
    *,
    caption: str | None = None,
    ask_kind: bool = False,
) -> str:
    """One prompt asking every collector's question of one comment (and, if asked, what kind of comment it is).

    The reel's title and caption say what the comment is replying to: on "Where should 4 friends meet in
    Paris?" a "Châtelet" answers the reel, it doesn't ask for a city.
    """
    blocks = []
    if ask_kind:
        kinds = "\n".join(f'  - "{k}": {desc}' for k, _, desc in KINDS)
        blocks.append(
            f'"kind": What kind of comment is it? Exactly one of:\n{kinds}\n  Pick the main one if several fit.'
        )
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
    keys = (['"kind"'] if ask_kind else []) + [f'"{_key(i)}"' for i in range(len(collectors))]
    shape = (
        '"kind" a string and the others lists of strings'
        if ask_kind and collectors
        else '"kind" a string'
        if ask_kind
        else "each a list of strings"
    )
    return f"""Read one Instagram comment and answer each question about it.
{_context(reel_title, caption)}
{chr(10).join(blocks)}

Rules:
- Answer only from what the comment itself says. A comment that doesn't answer a question gets an empty list for it.
- Praise, emoji, jokes and replies to other people answer nothing unless they really do answer the question.
- Something mentioned in passing is not an answer: only count what the commenter is asking for or telling us.
- A reply to the reel's own question (its title) is not an answer to these questions unless it really answers them.

Comment: {json.dumps(comment, ensure_ascii=False)}

Return a JSON object with exactly the keys {", ".join(keys)}: {shape}. JSON only."""


def parse_kind(reply: Mapping[str, Any]) -> str:
    """The comment's kind from the model's reply; anything unknown is "other"."""
    raw = reply.get("kind")
    kind = raw.strip().lower() if isinstance(raw, str) else ""
    return kind if kind in KIND_IDS else "other"


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


# Likes are read at a few ages and then left: early to catch a fast climber, later once they've settled.
# Five reads per comment, 25 comments per Graph call: ~0.2 calls per comment, ~100 a day at 500 comments.
LIKES_CHECKPOINTS: tuple[timedelta, ...] = (
    timedelta(hours=6),
    timedelta(days=1),
    timedelta(days=3),
    timedelta(days=7),
    timedelta(days=14),
)
# A comment is looked at for its last read up to a day after its last checkpoint (the worker may be down).
LIKES_WINDOW = LIKES_CHECKPOINTS[-1] + timedelta(days=1)


def likes_due(commented_at: datetime, likes_at: datetime | None, now: datetime) -> bool:
    """Whether a comment has passed a checkpoint since its likes were last read."""
    written = assume_utc(commented_at)
    age = assume_utc(now) - written
    if age >= LIKES_WINDOW:
        return False
    passed = [c for c in LIKES_CHECKPOINTS if age >= c]
    if not passed:
        return False
    if likes_at is None:
        return True
    return assume_utc(likes_at) - written < passed[-1]


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
