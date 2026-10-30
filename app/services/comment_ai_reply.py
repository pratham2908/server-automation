"""Per-channel AI replies to Instagram comments, answered from Meta's comment webhook.

Pure: no I/O. The store and worker (``comment_ai_reply_store`` / ``comment_ai_reply_worker``) do the
reading, queueing and sending; this holds what is decided about a comment and when.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from pydantic import BaseModel, Field, field_validator

from app.services.comment_dm import SKIP_BEFORE_RULE, SKIP_OWN, SKIP_TOO_OLD, CommentEvent
from app.timezone import assume_utc

INSTRUCTIONS_LIMIT = 1500
# A public reply has no window of its own, but a reply to a months-old comment reads as a bot.
MAX_COMMENT_AGE = timedelta(days=7)

STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

SKIP_NESTED = "a reply inside a thread"
SKIP_OWN_DM_REPLY = "comment-to-DM already replies to it"
SKIP_ALREADY_ANSWERED = "already answered"


class AiReplyConfig(BaseModel):
    """What the owner sets for a channel."""

    enabled: bool = False
    # Written by the owner and added to the prompt: tone, things to mention, things never to say.
    instructions: str = Field(default="", max_length=INSTRUCTIONS_LIMIT)
    delay_seconds: int = Field(default=60, ge=0, le=3600)

    @field_validator("instructions")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        return value.strip()


class StoredAiReply(AiReplyConfig):
    """The config as saved, with when it was last switched on."""

    # Comments written before this are left alone, so switching it on never answers a week of old comments.
    enabled_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("enabled_at", "updated_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        # Mongo hands datetimes back naive (UTC); comparing one with a comment's aware timestamp raises.
        return assume_utc(value) if value is not None else None


def stored_config(config: AiReplyConfig, previous: StoredAiReply | None, now: datetime) -> StoredAiReply:
    """*config* ready to save: ``enabled_at`` moves to now only when it goes from off (or absent) to on."""
    was_on = previous is not None and previous.enabled
    if not config.enabled:
        enabled_at = None
    elif was_on and previous is not None:
        enabled_at = previous.enabled_at
    else:
        enabled_at = now
    return StoredAiReply(**config.model_dump(), enabled_at=enabled_at, updated_at=now)


def skip_reason(
    event: CommentEvent,
    *,
    own_ids: set[str],
    own_username: str,
    active_since: datetime | None,
    now: datetime,
) -> str | None:
    """Why this comment must not get an AI reply, or None when it may."""
    if (event.from_id and event.from_id in own_ids) or (
        own_username and event.username.lower() == own_username.lower()
    ):
        return SKIP_OWN
    if event.parent_id:
        return SKIP_NESTED
    if event.created_at is not None:
        if active_since is not None and event.created_at < active_since:
            return SKIP_BEFORE_RULE
        if now - event.created_at > MAX_COMMENT_AGE:
            return SKIP_TOO_OLD
    return None


def reply_delay(config: AiReplyConfig, rng: random.Random) -> timedelta:
    """The configured wait, stretched or shrunk a little so replies don't land on a fixed beat."""
    if config.delay_seconds == 0:
        return timedelta(0)
    return timedelta(seconds=config.delay_seconds * rng.uniform(0.75, 1.5))
