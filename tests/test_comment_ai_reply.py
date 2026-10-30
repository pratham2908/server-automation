"""AI comment replies: when a comment may be answered and when."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from app.services.comment_ai_reply import (
    SKIP_NESTED,
    AiReplyConfig,
    StoredAiReply,
    reply_delay,
    skip_reason,
    stored_config,
)
from app.services.comment_dm import SKIP_BEFORE_RULE, SKIP_OWN, SKIP_TOO_OLD, CommentEvent

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _event(**kw: object) -> CommentEvent:
    base = {"comment_id": "c1", "media_id": "m1", "text": "love this", "username": "fan", "created_at": NOW}
    return CommentEvent.model_validate({**base, **kw})


def _reason(event: CommentEvent, since: datetime | None = None, threads: bool = False) -> str | None:
    return skip_reason(
        event, own_ids={"99"}, own_username="mine", active_since=since, now=NOW, reply_in_threads=threads
    )


def test_plain_comment_may_be_answered() -> None:
    assert _reason(_event()) is None


def test_own_nested_and_old_comments_are_skipped() -> None:
    assert _reason(_event(from_id="99")) == SKIP_OWN
    assert _reason(_event(username="Mine")) == SKIP_OWN
    assert _reason(_event(parent_id="p")) == SKIP_NESTED
    assert _reason(_event(created_at=NOW - timedelta(days=8))) == SKIP_TOO_OLD


def test_thread_replies_are_answered_only_when_the_channel_allows_it() -> None:
    nested = _event(parent_id="p")
    assert _reason(nested, threads=True) is None
    assert _reason(nested, threads=False) == SKIP_NESTED


def test_comments_before_switch_on_are_left_alone() -> None:
    assert _reason(_event(created_at=NOW - timedelta(hours=1)), since=NOW) == SKIP_BEFORE_RULE


def test_enabled_at_moves_only_when_switched_on() -> None:
    on = AiReplyConfig(enabled=True)
    first = stored_config(on, None, NOW)
    assert first.enabled_at == NOW
    later = NOW + timedelta(hours=1)
    assert stored_config(on, first, later).enabled_at == NOW
    off = stored_config(AiReplyConfig(enabled=False), first, later)
    assert off.enabled_at is None
    assert stored_config(on, off, later + timedelta(hours=1)).enabled_at == later + timedelta(hours=1)


def test_instructions_are_trimmed_and_delay_jitters_within_bounds() -> None:
    assert AiReplyConfig(instructions="  be warm  ").instructions == "be warm"
    delay = reply_delay(StoredAiReply(delay_seconds=100), random.Random(1))
    assert timedelta(seconds=75) <= delay <= timedelta(seconds=150)
    assert reply_delay(AiReplyConfig(delay_seconds=0), random.Random(1)) == timedelta(0)
