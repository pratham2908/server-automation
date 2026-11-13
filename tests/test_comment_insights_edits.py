from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.services import comment_collector_store as store
from app.services import comment_collectors as cc
from app.services import comment_translate as ct
from tests.mongo_fakes import DB

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


def collector() -> cc.Collector:
    return cc.Collector(
        name="Cities",
        question="Which city?",
        id="city",
        channel_id="map",
        active_from=NOW - timedelta(days=1),
        created_at=NOW,
        updated_at=NOW,
    )


def test_translation_reply_is_parsed():
    out = ct.parse_translation({"language": "Hinglish", "english": "Make one for Delhi"}, "Delhi ka banao")
    assert out == {"language": "Hinglish", "english": "Make one for Delhi", "already_english": False}
    same = ct.parse_translation({"language": "English", "english": "Nice"}, "Nice")
    assert same["already_english"] is True
    with pytest.raises(ValueError):
        ct.parse_translation({"language": "Hindi"}, "x")


def test_answer_done_ticks_and_unticks():
    db = DB()
    c = collector()
    run(db.comment_collectors.insert_one({**c.model_dump(), "id": "city"}))
    assert run(store.set_answer_done(db, c, "New York", True, NOW)) == ["new york"]
    assert run(store.set_answer_done(db, c, "new  york", True, NOW)) == ["new york"]
    assert run(store.set_answer_done(db, c, "New York", False, NOW)) == []
    with pytest.raises(ValueError):
        run(store.set_answer_done(db, c, "  ", True, NOW))


def test_kind_correction_sticks_to_a_sorted_comment_only():
    db = DB()
    run(db.comment_log.insert_one({"channel_id": "map", "comment_id": "1", "kind": "humour"}))
    run(db.comment_log.insert_one({"channel_id": "map", "comment_id": "2"}))
    assert run(store.set_kind_by_hand(db, "map", "1", "request", NOW)) is True
    row = run(db.comment_log.find_one({"comment_id": "1"}))
    assert row["kind"] == "request" and row["kind_by_hand"] is True
    assert run(store.set_kind_by_hand(db, "map", "2", "request", NOW)) is False
    assert run(store.set_kind_by_hand(db, "other", "1", "request", NOW)) is False
