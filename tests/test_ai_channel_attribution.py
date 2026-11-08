"""AI call cost attribution to channels, and the multi-select filters over it."""

import asyncio
from types import SimpleNamespace

import pytest

from app.routers.observability import UNATTRIBUTED_CHANNEL, _ai_log_match
from app.services.ai_call_logger import bind_ai_logger_db, log_ai_call
from app.services.ai_channel_context import (
    ai_channel_scope,
    channel_from_path,
    current_ai_channel,
    with_ai_channel,
)


class _Collection:
    def __init__(self):
        self.docs: list[dict] = []

    async def insert_one(self, doc):
        self.docs.append(doc)
        return SimpleNamespace(inserted_id="x")


@pytest.fixture
def db():
    fake = SimpleNamespace(ai_call_logs=_Collection())
    bind_ai_logger_db(fake)
    yield fake
    bind_ai_logger_db(None)


def test_channel_from_path():
    assert channel_from_path("/api/v1/channels/UC123/videos") == "UC123"
    assert channel_from_path("/api/v1/observability/ai-costs/summary") is None


@pytest.mark.asyncio
async def test_call_logged_inside_scope_carries_channel(db):
    with ai_channel_scope("UC1"):
        await log_ai_call("comment_reply", "m", 1, 2, 3.0, True, 0.01)
    await log_ai_call("comment_reply", "m", 1, 2, 3.0, True, 0.01)
    assert [d["channel_id"] for d in db.ai_call_logs.docs] == ["UC1", None]


@pytest.mark.asyncio
async def test_scope_resets_after_exit():
    with ai_channel_scope("UC1"):
        assert current_ai_channel() == "UC1"
    assert current_ai_channel() is None


@pytest.mark.asyncio
async def test_scope_follows_into_threads_and_tasks():
    with ai_channel_scope("UC9"):
        in_thread = await asyncio.to_thread(current_ai_channel)
        in_task = await asyncio.create_task(_read())
    assert (in_thread, in_task) == ("UC9", "UC9")


async def _read():
    return current_ai_channel()


@pytest.mark.asyncio
async def test_decorator_by_argument_name_and_resolver():
    @with_ai_channel("channel_id")
    async def by_name(video_id, channel_id):
        return current_ai_channel()

    @with_ai_channel(lambda item=None, **_: item["channel_id"])
    async def by_item(db, item):
        return current_ai_channel()

    assert await by_name("v", channel_id="UC2") == "UC2"
    assert await by_name("v", "UC3") == "UC3"
    assert await by_item(None, {"channel_id": "UC4"}) == "UC4"
    assert current_ai_channel() is None


def test_match_empty_means_no_filter():
    assert _ai_log_match() == {}


def test_match_channels_tasks_models():
    assert _ai_log_match(channels=["UC1", "UC2"], tasks=["a"], models=["m"]) == {
        "$or": [{"channel_id": {"$in": ["UC1", "UC2"]}}],
        "task": {"$in": ["a"]},
        "model": {"$in": ["m"]},
    }


def test_match_unattributed_alone_and_mixed():
    assert _ai_log_match(channels=[UNATTRIBUTED_CHANNEL]) == {"$or": [{"channel_id": None}]}
    assert _ai_log_match(channels=["UC1", UNATTRIBUTED_CHANNEL])["$or"] == [
        {"channel_id": {"$in": ["UC1"]}},
        {"channel_id": None},
    ]
