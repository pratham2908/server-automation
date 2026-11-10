"""The AI reply worker: what it posts, drafts and skips, with an in-memory database."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from app.services import comment_reply_guard as guard
from app.services.comment_ai_reply_worker import answer_one
from tests.mongo_fakes import DB

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


class Db(DB):
    def __init__(self, mode: str | None = None, enabled: bool = True) -> None:
        channel = {
            "channel_id": "ch",
            "comment_reply_mode": mode,
            "comment_ai_reply": {"enabled": enabled, "instructions": "be warm", "delay_seconds": 0},
        }
        super().__init__(
            channels=[channel],
            videos=[{"channel_id": "ch", "instagram_media_id": "m1", "title": "T", "video_id": "v1"}],
            comment_replies=[],
            comment_ai_reply_queue=[{"comment_id": "c1", "status": "sending"}],
        )


class Gemini:
    def __init__(self, sentiment: str, reply: str = "thanks!", explicit: bool = False) -> None:
        self.sentiment, self.reply, self.instructions, self.explicit = sentiment, reply, None, explicit
        self.generated = 0

    async def classify_comment_sentiment(
        self, comments: list[dict[str, Any]], video_context: str = ""
    ) -> list[dict[str, Any]]:
        return [
            {"comment_id": c["comment_id"], "sentiment": self.sentiment, "explicit": self.explicit} for c in comments
        ]

    async def generate_comment_reply(self, **kw: Any) -> str:
        self.generated += 1
        self.instructions = kw["instructions"]
        return self.reply


class Service:
    def __init__(self, hide_error: Exception | None = None) -> None:
        self.posted: list[tuple[str, str]] = []
        self.hidden: list[str] = []
        self.hide_error = hide_error

    def hide_comment(self, comment_id: str) -> bool:
        if self.hide_error:
            raise self.hide_error
        self.hidden.append(comment_id)
        return True

    def reply_to_comment(self, comment_id: str, text: str) -> str:
        self.posted.append((comment_id, text))
        return "r1"


class Manager:
    def __init__(self, service: Service) -> None:
        self.service = service

    async def get_service(self, channel_id: str, prefer: str | None = None) -> Service:
        return self.service


ITEM = {"comment_id": "c1", "channel_id": "ch", "media_id": "m1", "text": "great reel", "username": "fan"}


def _run(db: Db, gemini: Gemini, service: Service) -> str:
    return asyncio.run(answer_one(db, Manager(service), gemini, dict(ITEM), NOW))  # type: ignore[arg-type]


def test_positive_comment_is_replied_to_with_the_owners_instructions() -> None:
    db, gemini, service = Db(), Gemini("positive"), Service()
    assert _run(db, gemini, service) == "done"
    assert service.posted == [("c1", "thanks!")]
    assert gemini.instructions == "be warm"
    assert db.comment_replies.docs[0]["status"] == "replied"


def test_negative_comment_is_skipped_and_recorded() -> None:
    db, service = Db(), Service()
    assert _run(db, Gemini("negative"), service) == "skipped"
    assert service.posted == []
    assert db.comment_replies.docs[0]["status"] == "skipped_negative"


def test_review_mode_drafts_instead_of_posting() -> None:
    db, service = Db(mode="review"), Service()
    assert _run(db, Gemini("neutral"), service) == "done"
    assert service.posted == []
    assert db.comment_replies.docs[0]["status"] == "pending_approval"


def test_switched_off_channel_and_empty_ai_reply_post_nothing() -> None:
    service = Service()
    assert _run(Db(enabled=False), Gemini("positive"), service) == "skipped"
    assert _run(Db(), Gemini("positive", reply=""), service) == "failed"
    assert service.posted == []


def test_a_creepy_comment_is_never_answered_even_in_review_mode() -> None:
    for mode in (None, "review"):
        db, gemini, service = Db(mode=mode), Gemini("creepy"), Service()
        assert _run(db, gemini, service) == "skipped"
        assert service.posted == [] and gemini.generated == 0 and service.hidden == []
        [row] = db.comment_replies.docs
        assert row["status"] == "skipped_creepy" and "hidden" not in row
        assert db.comment_ai_reply_queue.docs[0]["skip_reason"] == "creepy comment"


def test_an_explicit_comment_is_hidden_and_the_hide_logged() -> None:
    db, service = Db(), Service()
    assert _run(db, Gemini("creepy", explicit=True), service) == "skipped"
    assert service.hidden == ["c1"] and service.posted == []
    row = db.comment_replies.docs[0]
    assert (row["status"], row["hidden"], row["explicit"]) == ("skipped_creepy", True, True)
    assert db.comment_ai_reply_queue.docs[0]["hidden"] is True


def test_a_failed_hide_still_skips_and_says_why() -> None:
    db, service = Db(), Service(hide_error=requests.HTTPError("nope"))
    assert _run(db, Gemini("creepy", explicit=True), service) == "skipped"
    row = db.comment_replies.docs[0]
    assert row["hidden"] is False and row["hide_error"] == "nope" and service.posted == []


def _replied(at: datetime, **who: str) -> dict[str, Any]:
    return {
        "channel_id": "ch",
        "status": "replied",
        "replied_at": at,
        "comment_id": f"old{at}",
        **who,
    }


def test_two_replies_a_day_per_person_then_the_cap() -> None:
    db, gemini, service = Db(), Gemini("positive"), Service()
    db.comment_replies.docs += [
        _replied(NOW - timedelta(hours=1), comment_author="fan"),
        _replied(NOW - timedelta(hours=23), comment_author_id="u9"),
    ]
    item = {**ITEM, "from_id": "u9"}
    assert asyncio.run(answer_one(db, Manager(service), gemini, item, NOW)) == "skipped"  # type: ignore[arg-type]
    assert service.posted == [] and gemini.generated == 0
    assert db.comment_replies.docs[-1]["status"] == "skipped_limit"
    assert db.comment_ai_reply_queue.docs[0]["skip_reason"] == "reply limit for this person"


def test_the_day_cap_rolls_and_the_week_cap_holds() -> None:
    # One reply 25 h ago: under both caps.
    db, service = Db(), Service()
    db.comment_replies.docs.append(_replied(NOW - timedelta(hours=25), comment_author="fan"))
    assert _run(db, Gemini("positive"), service) == "done"
    # Seven this week, none today: the week cap stops it.
    db, service = Db(), Service()
    db.comment_replies.docs += [_replied(NOW - timedelta(days=d, hours=2), comment_author="fan") for d in range(1, 7)]
    db.comment_replies.docs.append(_replied(NOW - timedelta(days=6, hours=20), comment_author="fan"))
    assert _run(db, Gemini("positive"), service) == "skipped" and service.posted == []


def test_other_peoples_and_other_channels_replies_dont_count() -> None:
    db, service = Db(), Service()
    db.comment_replies.docs += [_replied(NOW - timedelta(hours=1), comment_author="someone")] * 3
    db.comment_replies.docs += [{**_replied(NOW - timedelta(hours=1), comment_author="fan"), "channel_id": "other"}] * 3
    assert _run(db, Gemini("positive"), service) == "done"


def test_over_cap_is_pure() -> None:
    hour = timedelta(hours=1)
    assert not guard.over_cap([NOW - hour], NOW)
    assert guard.over_cap([NOW - hour, NOW - 2 * hour], NOW)
    assert not guard.over_cap([NOW - timedelta(days=1, hours=1)] * 6, NOW)
    assert guard.over_cap([NOW - timedelta(days=1, hours=1)] * 7, NOW)
    assert not guard.over_cap([NOW - timedelta(days=8)] * 9, NOW)


def test_a_reply_that_invents_plans_is_held_for_review_not_posted() -> None:
    """T0858: in auto mode a reply hinting at the channel's plans waits for a person."""
    reply = "haha thanks! it's a custom map tool we built, still figuring out if we should make it public 👀"
    db, service = Db(), Service()
    assert _run(db, Gemini("positive", reply=reply), service) == "done"
    assert service.posted == []
    row = db.comment_replies.docs[0]
    assert row["status"] == "pending_approval" and row["held_for"]
    assert db.comment_ai_reply_queue.docs[0]["held_for"] == row["held_for"]
