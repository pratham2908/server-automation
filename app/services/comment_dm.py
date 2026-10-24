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

from app.timezone import IST, assume_utc

PRIVATE_REPLY_WINDOW = timedelta(days=7)
# A queued DM still has to go out before Meta's window shuts, retries included.
ENQUEUE_WINDOW = PRIVATE_REPLY_WINDOW - timedelta(hours=6)
# Meta allows ~750 private replies an hour per account; staying far below means a reel taking off,
# plus retries, never reaches the ceiling.
HOURLY_CAP = 200
MAX_ATTEMPTS = 3
RETRY_BACKOFF = timedelta(minutes=5)
# Graph error codes that mean "too many calls, try later" rather than "this will never work".
RATE_LIMIT_CODES = frozenset({4, 17, 32, 613})

MAX_TRIGGERS = 20
MAX_REPLY_TEXTS = 10
DM_TEXT_LIMIT = 1000  # Instagram's cap on one message
REPLY_TEXT_LIMIT = 300
# Instagram's button template: at most 3 buttons, 20-character labels, and 640 characters of text.
MAX_BUTTONS = 3
BUTTON_TITLE_LIMIT = 20
BUTTON_TEXT_LIMIT = 640

STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
EventStatus = Literal["pending", "sending", "sent", "failed", "skipped"]

ANY_COMMENT = "*"


class DmButton(BaseModel):
    """A tappable link under the DM's text."""

    title: str = Field(min_length=1, max_length=BUTTON_TITLE_LIMIT)
    url: str = Field(max_length=2000, pattern=r"^https?://\S+$")

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("button title must not be blank")
        return stripped


class DmRule(BaseModel):
    """What one reel (or a channel's default) does with matching comments."""

    enabled: bool = True
    # Empty means every comment triggers; otherwise a comment must contain one of these words or phrases.
    triggers: list[str] = Field(default_factory=list, max_length=MAX_TRIGGERS)
    dm_text: str = Field(min_length=1, max_length=DM_TEXT_LIMIT)
    # One is picked at random per comment, so a burst of replies doesn't read as one pasted line.
    # Empty means no public reply, only the DM.
    reply_texts: list[str] = Field(default_factory=list, max_length=MAX_REPLY_TEXTS)
    # Roughly how long after the comment the DM goes out; jittered so it doesn't land instantly.
    delay_seconds: int = Field(default=45, ge=0, le=3600)
    # Links shown as buttons under the text. Empty sends plain text.
    buttons: list[DmButton] = Field(default_factory=list, max_length=MAX_BUTTONS)

    @model_validator(mode="after")
    def _buttons_fit_the_template(self) -> DmRule:
        if self.buttons and len(self.dm_text) > BUTTON_TEXT_LIMIT:
            raise ValueError(f"with buttons, the message must be at most {BUTTON_TEXT_LIMIT} characters")
        return self

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
        stripped = value.strip()
        if not stripped:
            raise ValueError("dm_text must not be blank")
        return stripped

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


def dm_message(text: str, buttons: list[DmButton]) -> dict[str, Any]:
    """The ``message`` object for a DM: plain text, or Instagram's button template when there are buttons."""
    if not buttons:
        return {"text": text}
    return {
        "attachment": {
            "type": "template",
            "payload": {
                "template_type": "button",
                "text": text,
                "buttons": [{"type": "web_url", "url": b.url, "title": b.title} for b in buttons],
            },
        }
    }


def text_with_links(text: str, buttons: list[DmButton]) -> str:
    """The fallback when Instagram won't take buttons on this message: the links written under the text."""
    if not buttons:
        return text
    lines = "\n".join(f"{b.title}: {b.url}" for b in buttons)
    return f"{text}\n\n{lines}"[:DM_TEXT_LIMIT]


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
        stamp = entry.get("time")
        created_at = datetime.fromtimestamp(stamp, tz=IST) if isinstance(stamp, int | float) else None
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
