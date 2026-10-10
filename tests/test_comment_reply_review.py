"""The review queue: drafts wait for a person, and nothing is sent until they approve.

The invariants worth pinning are the ones that cost something when wrong: a draft
that posts without approval, an approve that posts twice, a reject that lets the
same comment be drafted again, and our own comments being answered.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services.comment_reply_approval import (
    PendingReplyNotFoundError,
    ReplyPostFailedError,
    approve_pending_reply,
    reject_pending_reply,
)
from app.services.comment_reply_engine import run_comment_reply_cycle
from app.services.comment_reply_review import (
    MODE_AUTO,
    MODE_REVIEW,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_REPLIED,
    is_own_comment,
    is_reviewable,
    normalise_mode,
    own_identities,
    validate_reply,
)
from app.timezone import now_ist

# --- pure rules ---------------------------------------------------------------


def test_unknown_or_missing_mode_keeps_the_old_auto_behaviour():
    """Channels created before this feature have no field and must not start queueing."""
    assert normalise_mode(None) == MODE_AUTO
    assert normalise_mode("bogus") == MODE_AUTO
    assert normalise_mode(MODE_REVIEW) == MODE_REVIEW


def test_spam_is_never_queued_but_everything_else_is():
    assert is_reviewable("positive") and is_reviewable("negative") and is_reviewable("neutral")
    assert not is_reviewable("spam")


def test_an_empty_author_is_unknown_not_somebody_elses():
    """Without instagram_manage_comments every author is blank; that must not match, nor crash."""
    ids = own_identities({"instagram_username": "physicsasmr"})
    assert not is_own_comment("", ids)


def test_own_comments_match_on_username_handle_or_name_ignoring_case_and_at_sign():
    ids = own_identities({"instagram_username": "PhysicsASMR", "handle": "@physics.asmr", "name": "Physics ASMR"})
    assert is_own_comment("physicsasmr", ids)
    assert is_own_comment("@Physics.ASMR", ids)
    assert is_own_comment("physics asmr", ids)
    assert not is_own_comment("someone_else", ids)


def test_validate_reply_refuses_blank_and_over_length_text():
    with pytest.raises(ValueError):
        validate_reply("   ", "instagram")
    with pytest.raises(ValueError):
        validate_reply("x" * 2201, "instagram")
    assert validate_reply("  thanks!  ", "instagram") == "thanks!"
    assert validate_reply("x" * 2201, "youtube")  # YouTube allows far longer


# --- a tiny in-memory comment_replies collection ------------------------------


class _Result:
    def __init__(self, matched: int) -> None:
        self.matched_count = matched


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        return list(self._docs)


def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, want in query.items():
        have = doc.get(key)
        if isinstance(want, dict) and "$in" in want:
            if have not in want["$in"]:
                return False
        elif have != want:
            return False
    return True


class _Replies:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return next((r for r in self.rows if _matches(r, query)), None)

    async def find_one_and_update(self, query: dict[str, Any], update: dict[str, Any]) -> dict[str, Any] | None:
        row = await self.find_one(query)
        if row:
            before = dict(row)
            row.update(update["$set"])
            return before
        return None

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> _Result:
        row = await self.find_one(query)
        if not row:
            return _Result(0)
        row.update(update.get("$set", {}))
        for key in update.get("$unset", {}):
            row.pop(key, None)
        return _Result(1)

    async def insert_one(self, doc: dict[str, Any]) -> None:
        self.rows.append(doc)

    def find(self, query: dict[str, Any], projection: dict[str, Any] | None = None) -> _Cursor:
        return _Cursor([r for r in self.rows if _matches(r, query)])


class _Db:
    def __init__(self, replies: _Replies) -> None:
        self.comment_replies = replies


def _pending(comment_id: str = "c1", **extra: Any) -> dict[str, Any]:
    return {
        "channel_id": "ch",
        "comment_id": comment_id,
        "platform": "instagram",
        "status": STATUS_PENDING,
        "suggested_reply": "Thanks!",
        "reply_text": "Thanks!",
        **extra,
    }


# --- approving and rejecting --------------------------------------------------


def test_approving_posts_the_draft_and_records_it_as_replied():
    replies = _Replies([_pending()])
    sent: list[tuple[str, str]] = []

    def post(comment_id: str, text: str) -> str:
        sent.append((comment_id, text))
        return "reply-1"

    row = asyncio.run(approve_pending_reply(_Db(replies), "ch", "c1", post))  # type: ignore[arg-type]
    assert sent == [("c1", "Thanks!")]
    assert row["status"] == STATUS_REPLIED and row["reply_id"] == "reply-1" and row["edited"] is False
    assert replies.rows[0]["status"] == STATUS_REPLIED


def test_an_edited_reply_is_what_gets_sent_and_is_flagged_as_edited():
    replies = _Replies([_pending()])
    sent: list[str] = []

    def post(comment_id: str, text: str) -> str:
        sent.append(text)
        return "r"

    row = asyncio.run(approve_pending_reply(_Db(replies), "ch", "c1", post, "Much better words"))  # type: ignore[arg-type]
    assert sent == ["Much better words"]
    assert row["edited"] is True and row["suggested_reply"] == "Thanks!"


def test_a_second_approve_cannot_post_the_reply_twice():
    replies = _Replies([_pending()])
    calls: list[str] = []

    def post(comment_id: str, text: str) -> str:
        calls.append(text)
        return "r"

    asyncio.run(approve_pending_reply(_Db(replies), "ch", "c1", post))  # type: ignore[arg-type]
    with pytest.raises(PendingReplyNotFoundError):
        asyncio.run(approve_pending_reply(_Db(replies), "ch", "c1", post))  # type: ignore[arg-type]
    assert len(calls) == 1


def test_a_platform_failure_returns_the_draft_to_the_queue_with_the_reason():
    replies = _Replies([_pending()])

    def post(comment_id: str, text: str) -> str:
        raise RuntimeError("Permission denied")

    with pytest.raises(ReplyPostFailedError):
        asyncio.run(approve_pending_reply(_Db(replies), "ch", "c1", post))  # type: ignore[arg-type]
    row = replies.rows[0]
    assert row["status"] == STATUS_PENDING and "Permission denied" in row["last_error"]


def test_an_invalid_edit_is_refused_before_anything_is_claimed_or_sent():
    replies = _Replies([_pending()])
    called: list[str] = []
    with pytest.raises(ValueError):
        asyncio.run(approve_pending_reply(_Db(replies), "ch", "c1", lambda c, t: called.append(t) or "r", "  "))  # type: ignore[arg-type]
    assert called == [] and replies.rows[0]["status"] == STATUS_PENDING


def test_rejecting_keeps_the_row_so_the_comment_is_not_drafted_again():
    replies = _Replies([_pending()])
    asyncio.run(reject_pending_reply(_Db(replies), "ch", "c1"))  # type: ignore[arg-type]
    assert replies.rows[0]["status"] == STATUS_REJECTED
    with pytest.raises(PendingReplyNotFoundError):
        asyncio.run(reject_pending_reply(_Db(replies), "ch", "c1"))  # type: ignore[arg-type]


# --- the cycle ----------------------------------------------------------------


class _Coll:
    def __init__(self, doc: dict[str, Any] | None = None, docs: list[dict[str, Any]] | None = None) -> None:
        self._doc, self._docs = doc, docs or []

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return self._doc

    def find(self, *args: Any, **kwargs: Any) -> Any:
        coll = self

        class _Chain:
            def sort(self, *a: Any) -> _Chain:
                return self

            def limit(self, *a: Any) -> _Chain:
                return self

            async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
                return list(coll._docs)

        return _Chain()


class _CycleDb:
    def __init__(self, channel: dict[str, Any], replies: _Replies) -> None:
        self.config = _Coll(None)
        self.channels = _Coll(channel)
        self.videos = _Coll(
            docs=[{"video_id": "v1", "title": "Cool reel", "instagram_media_id": "m1", "published_at": now_ist()}]
        )
        self.comment_replies = replies


class _Ig:
    def __init__(self, comments: list[dict[str, Any]]) -> None:
        self._comments = comments
        self.replied: list[tuple[str, str]] = []

    def get_media_comments(self, media_id: str) -> list[dict[str, Any]]:
        return self._comments

    def reply_to_comment(self, comment_id: str, message: str) -> str:
        self.replied.append((comment_id, message))
        return "r"


class _Manager:
    def __init__(self, svc: _Ig) -> None:
        self._svc = svc

    async def get_service(self, channel_id: str, prefer: str | None = None) -> _Ig:
        return self._svc


class _Gemini:
    def __init__(self, sentiments: dict[str, str], fail_reply_for: set[str] | None = None) -> None:
        self._sentiments = sentiments
        self._fail = fail_reply_for or set()
        self.reply_sentiments: list[str] = []
        self.contexts: list[str] = []

    async def classify_comment_sentiment(
        self, batch: list[dict[str, Any]], video_context: str = ""
    ) -> list[dict[str, Any]]:
        self.contexts.append(video_context)
        return [{"comment_id": c["comment_id"], "sentiment": self._sentiments[c["comment_id"]]} for c in batch]

    async def generate_comment_reply(
        self, comment_text: str, video_title: str, platform: str, sentiment: str, video_context: str = ""
    ) -> str:
        self.contexts.append(video_context)
        self.reply_sentiments.append(sentiment)
        return "" if comment_text in self._fail else f"draft for: {comment_text}"


def _comments() -> list[dict[str, Any]]:
    mk = lambda i, text, author: {"comment_id": i, "text": text, "author": author}  # noqa: E731
    return [
        mk("pos", "love it", "fan1"),
        mk("neg", "this is wrong", "critic"),
        mk("neu", "what camera?", "curious"),
        mk("spam", "follow me", "bot"),
        mk("own", "thanks all", "physicsasmr"),
    ]


SENTIMENTS = {"pos": "positive", "neg": "negative", "neu": "neutral", "spam": "spam", "own": "positive"}


def _run(mode: str | None, gemini: _Gemini) -> tuple[dict[str, Any], _Replies, _Ig]:
    replies = _Replies()
    ig = _Ig(_comments())
    channel = {"channel_id": "ch", "platform": "instagram", "instagram_username": "physicsasmr"}
    if mode:
        channel["comment_reply_mode"] = mode
    stats = asyncio.run(
        run_comment_reply_cycle("ch", _CycleDb(channel, replies), None, _Manager(ig), gemini)  # type: ignore[arg-type]
    )
    return stats, replies, ig


def test_review_mode_drafts_positive_negative_and_neutral_and_sends_nothing():
    stats, replies, ig = _run(MODE_REVIEW, _Gemini(SENTIMENTS))
    by_id = {r["comment_id"]: r for r in replies.rows}
    assert ig.replied == []
    assert {c for c, r in by_id.items() if r["status"] == STATUS_PENDING} == {"pos", "neg", "neu"}
    assert by_id["spam"]["status"] == "skipped_spam"
    assert "own" not in by_id  # our own comment is never answered
    assert stats["drafted"] == 3 and stats["replied"] == 0


def test_negative_and_neutral_drafts_are_generated_without_the_subscribe_pitch():
    gemini = _Gemini(SENTIMENTS)
    _run(MODE_REVIEW, gemini)
    assert sorted(gemini.reply_sentiments) == ["negative", "neutral", "positive"]


def test_auto_mode_still_replies_only_to_positive_comments():
    stats, replies, ig = _run(MODE_AUTO, _Gemini(SENTIMENTS))
    assert [c for c, _ in ig.replied] == ["pos"]
    assert stats["drafted"] == 0 and stats["replied"] == 1
    assert {r["comment_id"]: r["status"] for r in replies.rows}["neg"] == "skipped_negative"


def test_a_channel_with_no_mode_set_behaves_as_auto():
    stats, _, ig = _run(None, _Gemini(SENTIMENTS))
    assert stats["drafted"] == 0 and len(ig.replied) == 1


def test_a_failed_ai_draft_for_a_complaint_is_retried_not_filled_with_a_template():
    """The canned replies are subscribe pitches; queueing one under a complaint would be worse than waiting."""
    stats, replies, _ = _run(MODE_REVIEW, _Gemini(SENTIMENTS, fail_reply_for={"this is wrong"}))
    ids = {r["comment_id"] for r in replies.rows}
    assert "neg" not in ids and stats["errors"] == 1


def test_a_cycle_rerun_does_not_draft_the_same_comments_again():
    replies = _Replies()
    channel = {
        "channel_id": "ch",
        "platform": "instagram",
        "instagram_username": "physicsasmr",
        "comment_reply_mode": MODE_REVIEW,
    }
    for _ in range(2):
        asyncio.run(
            run_comment_reply_cycle(
                "ch",
                _CycleDb(channel, replies),
                None,
                _Manager(_Ig(_comments())),
                _Gemini(SENTIMENTS),  # type: ignore[arg-type]
            )
        )
    assert [r["comment_id"] for r in replies.rows].count("pos") == 1


def test_the_engine_gives_the_model_the_videos_title_and_description():
    """The classifier and the reply writer both see what the video is, so a comment is read in context."""
    replies = _Replies()
    ig = _Ig(_comments())
    channel = {
        "channel_id": "ch",
        "platform": "instagram",
        "instagram_username": "physicsasmr",
        "comment_reply_mode": MODE_REVIEW,
    }
    db = _CycleDb(channel, replies)
    db.videos = _Coll(
        docs=[
            {
                "video_id": "v1",
                "title": "Cool reel",
                "description": "How a gyroscope resists tilting",
                "instagram_media_id": "m1",
                "published_at": now_ist(),
            }
        ]
    )
    gemini = _Gemini(SENTIMENTS)
    asyncio.run(run_comment_reply_cycle("ch", db, None, _Manager(ig), gemini))  # type: ignore[arg-type]
    assert gemini.contexts, "the model was never called"
    assert all("Cool reel" in c and "gyroscope" in c for c in gemini.contexts)
