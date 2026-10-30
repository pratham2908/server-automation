"""The AI reply worker: what it posts, drafts and skips, with an in-memory database."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.services.comment_ai_reply_worker import answer_one

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


class Coll:
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs = docs or []

    async def find_one(self, query: dict[str, Any], projection: Any = None) -> dict[str, Any] | None:
        return next((d for d in self.docs if all(d.get(k) == v for k, v in query.items())), None)

    async def insert_one(self, doc: dict[str, Any]) -> None:
        self.docs.append(doc)

    async def update_one(self, query: dict[str, Any], change: dict[str, Any]) -> None:
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(change["$set"])


class Db:
    def __init__(self, mode: str | None = None, enabled: bool = True) -> None:
        channel = {
            "channel_id": "ch",
            "comment_reply_mode": mode,
            "comment_ai_reply": {"enabled": enabled, "instructions": "be warm", "delay_seconds": 0},
        }
        self.channels = Coll([channel])
        self.videos = Coll([{"channel_id": "ch", "instagram_media_id": "m1", "title": "T", "video_id": "v1"}])
        self.comment_replies = Coll()
        self.comment_ai_reply_queue = Coll([{"comment_id": "c1", "status": "sending"}])


class Gemini:
    def __init__(self, sentiment: str, reply: str = "thanks!") -> None:
        self.sentiment, self.reply, self.instructions = sentiment, reply, None

    async def classify_comment_sentiment(
        self, comments: list[dict[str, Any]], video_context: str = ""
    ) -> list[dict[str, Any]]:
        return [{"comment_id": c["comment_id"], "sentiment": self.sentiment} for c in comments]

    async def generate_comment_reply(self, **kw: Any) -> str:
        self.instructions = kw["instructions"]
        return self.reply


class Service:
    def __init__(self) -> None:
        self.posted: list[tuple[str, str]] = []

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
