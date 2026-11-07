"""Comment-to-DM: when someone comments a trigger word on a reel, DM them once and reply to the comment.

Pure rules only — matching, eligibility, timing, reading Meta's webhook — so they are testable without
Mongo or Meta. Storage lives in ``comment_dm_store.py``, the sending loop in ``comment_dm_worker.py``.

Meta's limits shape all of it: a comment can be answered with exactly one private reply, and only
within 7 days of being written; anything further is allowed only after the person writes back.
"""

from __future__ import annotations

import hashlib
import hmac
import random
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.dm_content import (
    BUTTON_TEXT_LIMIT,
    MAX_BUTTONS,
    MAX_CARDS,
    MAX_QUICK_REPLIES,
    DmButton,
    DmCard,
    DmMessage,
    DmQuickReply,
)
from app.services.meet_city import CityMap, sample_comment_dm
from app.timezone import IST, assume_utc

PRIVATE_REPLY_WINDOW = timedelta(days=7)
# A queued DM still has to go out before Meta's window shuts, retries included.
ENQUEUE_WINDOW = PRIVATE_REPLY_WINDOW - timedelta(hours=6)
# Meta allows ~750 private replies an hour per account. 500 leaves room for retries under that
# ceiling without holding a viral reel's DMs back (owner, 2026-11-04: was 200).
HOURLY_CAP = 500
MAX_ATTEMPTS = 3
RETRY_BACKOFF = timedelta(minutes=5)
# Graph error codes that mean "too many calls, try later" rather than "this will never work".
RATE_LIMIT_CODES = frozenset({4, 17, 32, 613})

MAX_TRIGGERS = 20
MAX_REPLY_TEXTS = 10
DM_TEXT_LIMIT = 1000  # Instagram's cap on one message
REPLY_TEXT_LIMIT = 300

STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
EventStatus = Literal["pending", "sending", "sent", "failed", "skipped"]

ANY_COMMENT = "*"


class DmRule(BaseModel):
    """What one reel (or a channel's default) does with matching comments."""

    enabled: bool = True
    # Empty means every comment triggers; otherwise a comment must contain one of these words or phrases.
    triggers: list[str] = Field(default_factory=list, max_length=MAX_TRIGGERS)
    # The DM's text. Empty only when the DM is a carousel of cards.
    dm_text: str = Field(default="", max_length=DM_TEXT_LIMIT)
    # One is picked at random per comment, so a burst of replies doesn't read as one pasted line.
    # Empty means no public reply, only the DM.
    reply_texts: list[str] = Field(default_factory=list, max_length=MAX_REPLY_TEXTS)
    # Roughly how long after the comment the DM goes out; jittered so it doesn't land instantly. Short,
    # because a comment deleted before the DM goes out can't be answered at all (owner, 2026-11-04: was 45;
    # 46 of TryAlgoViz's first 461 failed that way, ~57 s after the comment).
    delay_seconds: int = Field(default=10, ge=0, le=3600)
    # Buttons under the text: a link, our profile, or a reply sent when tapped. Empty sends plain text.
    buttons: list[DmButton] = Field(default_factory=list, max_length=MAX_BUTTONS)
    # A carousel instead of text and buttons.
    cards: list[DmCard] = Field(default_factory=list, max_length=MAX_CARDS)
    # Chips under the text; a tap counts as them writing back, which opens Instagram's 24-hour window.
    quick_replies: list[DmQuickReply] = Field(default_factory=list, max_length=MAX_QUICK_REPLIES)
    # Set: the DM is built from the city each comment names (see ``meet_city``), and the text, buttons,
    # cards and chips above are unused. A comment that names no place gets no DM and no public reply.
    city_map: CityMap | None = None
    # Set: nothing is ever sent. Each comment's city is read (the model reads the ones a plain match
    # can't) and kept in the tally, ``dm_city_answers``, with no DM and no public reply. For a city vote
    # ("comment your city") once the map is no longer given away (owner via @themapworld, 2026-11-07).
    tally_only: bool = False

    @model_validator(mode="after")
    def _is_one_message(self) -> DmRule:
        if self.tally_only:
            return self
        if self.buttons and len(self.dm_text) > BUTTON_TEXT_LIMIT:
            raise ValueError(f"with buttons, the message must be at most {BUTTON_TEXT_LIMIT} characters")
        # A private reply is exactly one message, so it carries no media; everything else must build.
        self.message()
        return self

    @property
    def reads_cities(self) -> bool:
        return self.city_map is not None or self.tally_only

    def message(self) -> DmMessage:
        """The DM this rule sends, as the shared content model. A city rule's DM depends on the comment,
        so its message here is the one London gets: enough to check the rule can be sent at all."""
        if self.city_map is not None:
            return sample_comment_dm(self.city_map)
        return DmMessage(text=self.dm_text, buttons=self.buttons, cards=self.cards, quick_replies=self.quick_replies)

    @field_validator("triggers")
    @classmethod
    def _clean_triggers(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for raw in value:
            term = raw.strip().lower()
            if term and term not in seen:
                seen.append(term)
        return seen

    @field_validator("dm_text")
    @classmethod
    def _strip_dm(cls, value: str) -> str:
        return value.strip()

    @field_validator("reply_texts")
    @classmethod
    def _clean_replies(cls, value: list[str]) -> list[str]:
        cleaned = [r.strip() for r in value if r.strip()]
        too_long = [r for r in cleaned if len(r) > REPLY_TEXT_LIMIT]
        if too_long:
            raise ValueError(f"each reply must be at most {REPLY_TEXT_LIMIT} characters")
        return cleaned


class StoredRule(BaseModel):
    """A rule as saved, with when it was last switched on."""

    rule: DmRule
    # Comments written before this are left alone, so switching a rule on never DMs a week of
    # old commenters in one go.
    enabled_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("enabled_at", "updated_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        # Mongo hands datetimes back naive (UTC); comparing one with a comment's aware timestamp raises.
        return assume_utc(value) if value is not None else None


def stored_rule(rule: DmRule, previous: StoredRule | None, now: datetime) -> StoredRule:
    """*rule* ready to save: ``enabled_at`` moves to now only when the rule goes from off (or absent) to on."""
    was_on = previous is not None and previous.rule.enabled
    if not rule.enabled:
        enabled_at = None
    elif was_on and previous is not None:
        enabled_at = previous.enabled_at
    else:
        enabled_at = now
    return StoredRule(rule=rule, enabled_at=enabled_at, updated_at=now)


class CommentEvent(BaseModel):
    """One comment as it reaches us, whether from Meta's webhook or our own poll."""

    comment_id: str
    media_id: str
    # The Instagram account the webhook was addressed to; empty when the comment came from a poll.
    account_id: str = ""
    text: str = ""
    from_id: str = ""
    username: str = ""
    parent_id: str | None = None
    created_at: datetime | None = None


def effective_rule(reel_rule: StoredRule | None, default: StoredRule | None) -> StoredRule | None:
    """The rule a reel runs. A reel's own rule wins — switched off, it silences the default too."""
    if reel_rule is not None:
        return reel_rule if reel_rule.rule.enabled else None
    return default if default is not None and default.rule.enabled else None


def _contains_term(text: str, term: str) -> bool:
    """*term* appears in *text* as a whole word or phrase, so "link" doesn't fire on "linked".

    Boundaries are "not a letter or digit" rather than regex ``\\b``, which has no notion of where an
    emoji trigger starts or ends.
    """
    start = text.find(term)
    while start != -1:
        end = start + len(term)
        before_ok = start == 0 or not text[start - 1].isalnum()
        after_ok = end == len(text) or not text[end].isalnum()
        if before_ok and after_ok:
            return True
        start = text.find(term, start + 1)
    return False


def matched_trigger(rule: DmRule, text: str) -> str | None:
    """The trigger this comment hit, ``"*"`` when the rule takes every comment, or None."""
    if not rule.triggers:
        return ANY_COMMENT
    lowered = text.lower()
    return next((t for t in rule.triggers if _contains_term(lowered, t)), None)


SKIP_OWN = "our own comment"
SKIP_BEFORE_RULE = "written before the rule was switched on"
SKIP_TOO_OLD = "too old for Instagram to allow a private reply"
SKIP_ALREADY_MESSAGED = "already messaged on this reel"
SKIP_TALLY_ONLY = "tally only: the city is kept, nothing is sent"
# Only this one is worth a line in the log: the others are either noise (our own comments) or
# would flood it the moment a rule is switched on for a busy reel.
LOGGED_SKIPS = frozenset({SKIP_ALREADY_MESSAGED})


def person_key(event: CommentEvent) -> str:
    """Who wrote the comment, stable across webhook and poll. Usernames are what both always carry."""
    return event.username.lower() if event.username else f"id:{event.from_id}"


def skip_reason(
    event: CommentEvent,
    *,
    own_ids: set[str],
    own_username: str,
    active_since: datetime | None,
    now: datetime,
    already_messaged: bool,
) -> str | None:
    """Why this comment must not get a DM, or None when it may."""
    if (event.from_id and event.from_id in own_ids) or (
        own_username and event.username.lower() == own_username.lower()
    ):
        return SKIP_OWN
    if event.created_at is not None:
        if active_since is not None and event.created_at < active_since:
            return SKIP_BEFORE_RULE
        if now - event.created_at > ENQUEUE_WINDOW:
            return SKIP_TOO_OLD
    if already_messaged:
        return SKIP_ALREADY_MESSAGED
    return None


def send_delay(rule: DmRule, rng: random.Random) -> timedelta:
    """The configured delay, stretched or shrunk a little so a burst of DMs isn't evenly spaced."""
    if rule.delay_seconds == 0:
        return timedelta(0)
    return timedelta(seconds=rule.delay_seconds * rng.uniform(0.75, 1.5))


def pick_reply(rule: DmRule, rng: random.Random) -> str | None:
    return rng.choice(rule.reply_texts) if rule.reply_texts else None


def is_rate_limited(code: int | None) -> bool:
    return code in RATE_LIMIT_CODES


def retry_at(attempts: int, now: datetime) -> datetime | None:
    """When to try a rate-limited send again, or None once it has used up its attempts."""
    if attempts >= MAX_ATTEMPTS:
        return None
    return now + RETRY_BACKOFF * attempts


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Meta signs each delivery with the app secret: ``X-Hub-Signature-256: sha256=<hex hmac of the body>``."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def _as_str(value: Any) -> str:
    return str(value) if value is not None else ""


def _entry_time(stamp: Any) -> datetime | None:
    """An entry's ``time``: seconds on comment deliveries, milliseconds on messaging ones."""
    if not isinstance(stamp, int | float) or stamp <= 0:
        return None
    seconds = stamp / 1000 if stamp > 1e11 else stamp
    return datetime.fromtimestamp(seconds, tz=IST)


def parse_comment_webhook(payload: dict[str, Any]) -> list[CommentEvent]:
    """The comment events in one webhook delivery; anything that isn't a comment is ignored.

    Shape: ``{"object": "instagram", "entry": [{"id": <account>, "time": <unix>, "changes":
    [{"field": "comments", "value": {"id", "text", "from": {"id", "username"}, "media": {"id"},
    "parent_id"?}}]}]}``. The comment itself carries no timestamp, so the entry's time stands in.
    """
    if payload.get("object") != "instagram":
        return []
    events: list[CommentEvent] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        account_id = _as_str(entry.get("id"))
        created_at = _entry_time(entry.get("time"))
        for change in entry.get("changes") or []:
            if not isinstance(change, dict) or change.get("field") != "comments":
                continue
            value = change.get("value") or {}
            sender = value.get("from") or {}
            media = value.get("media") or {}
            comment_id = _as_str(value.get("id"))
            media_id = _as_str(media.get("id"))
            if not comment_id or not media_id:
                continue
            events.append(
                CommentEvent(
                    comment_id=comment_id,
                    media_id=media_id,
                    account_id=account_id,
                    text=_as_str(value.get("text")),
                    from_id=_as_str(sender.get("id")),
                    username=_as_str(sender.get("username")),
                    parent_id=_as_str(value.get("parent_id")) or None,
                    created_at=created_at,
                )
            )
    return events
