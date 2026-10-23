"""The Threads publisher: posts a due video, retries, and bounces back with the reason."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.services import threads_publisher as tp
from app.timezone import now_ist
from tests.mongo_fakes import DB


class FakeR2:
    def __init__(self, exists: bool = True) -> None:
        self.exists = exists

    def file_exists(self, key: str) -> bool:
        return self.exists

    def generate_presigned_url(self, key: str, expires_in: int) -> str:
        return f"https://r2/{key}"


class FakeThreads:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.posted: list[tuple[str, str, str | None]] = []
        self.comments: list[tuple[str, str]] = []

    def publish_video(self, url: str, text: str, topic: str | None) -> str:
        if self.fail:
            raise self.fail
        self.posted.append((url, text, topic))
        return "th_1"

    def get_permalink(self, media_id: str) -> str:
        return "https://www.threads.net/@why_the_map/post/abc"

    def post_comment(self, media_id: str, message: str) -> str:
        self.comments.append((media_id, message))
        return "reply_1"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    sent: list[dict[str, Any]] = []

    async def fake_notify(db, video, marker, scheduled_at):
        sent.append(marker)
        return True

    async def noop(*a, **k):
        return None

    class _Errors:
        async def log_error(self, **kwargs):
            return None

    monkeypatch.setattr(tp, "notify_publish_failure", fake_notify)
    monkeypatch.setattr(tp, "update_channel_task_status", noop)
    monkeypatch.setattr(tp, "get_error_service", lambda db: _Errors())
    return sent


def _db(**video: Any) -> DB:
    return DB(
        channels=[{"channel_id": "wtm-th", "platform": "threads", "threads_tokens": {"access_token": "t"}}],
        videos=[
            {
                "channel_id": "wtm-th",
                "video_id": "v1",
                "title": "Gibraltar",
                "description": "Why Gibraltar is British",
                "tags": ["Geography"],
                "status": "queued",
                "r2_object_key": "k.mp4",
                **video,
            }
        ],
        schedule_queue=[
            {
                "channel_id": "wtm-th",
                "video_id": "v1",
                "platform": "threads",
                "scheduled_at": now_ist() - timedelta(minutes=1),
            },
            # Another platform's entry must be left for its own publisher.
            {
                "channel_id": "ig",
                "video_id": "v2",
                "platform": "instagram",
                "scheduled_at": now_ist() - timedelta(minutes=1),
            },
        ],
    )


async def _run(monkeypatch, db: DB, service: FakeThreads | None, r2: FakeR2 | None = None) -> None:
    async def get_service(self, channel_id):
        return service

    monkeypatch.setattr(tp.ThreadsServiceManager, "get_service", get_service)
    await tp._poll_and_publish(db, r2 or FakeR2())


@pytest.mark.asyncio
async def test_a_due_video_is_posted_with_its_text_and_topic(monkeypatch) -> None:
    db = _db(first_comment="Which one next?")
    service = FakeThreads()
    await _run(monkeypatch, db, service)

    assert service.posted == [("https://r2/k.mp4", "Why Gibraltar is British", "Geography")]
    video = db.videos.docs[0]
    assert video["status"] == "published"
    assert video["threads_media_id"] == "th_1"
    assert video["threads_permalink"].startswith("https://www.threads.net/")
    assert service.comments == [("th_1", "Which one next?")]
    assert [e["platform"] for e in db.schedule_queue.docs] == ["instagram"]


@pytest.mark.asyncio
async def test_a_failure_is_retried_then_bounced_with_the_reason(monkeypatch, _quiet) -> None:
    db = _db()
    service = FakeThreads(fail=RuntimeError("Threads container c1 error: INVALID_DURATION"))
    for _ in range(4):
        await _run(monkeypatch, db, service)
    assert db.videos.docs[0]["status"] == "queued"
    assert db.schedule_queue.docs[0]["attempt_count"] == 4

    await _run(monkeypatch, db, service)
    video = db.videos.docs[0]
    assert video["status"] == "ready"
    assert video["last_failure"]["platform"] == "threads"
    assert "INVALID_DURATION" in video["last_failure"]["detail"]
    assert len(_quiet) == 1


@pytest.mark.asyncio
async def test_a_missing_file_bounces_at_once(monkeypatch, _quiet) -> None:
    db = _db()
    await _run(monkeypatch, db, FakeThreads(), FakeR2(exists=False))
    assert db.videos.docs[0]["status"] == "ready"
    assert len(_quiet) == 1


@pytest.mark.asyncio
async def test_no_token_bounces_with_a_reconnect_reason(monkeypatch, _quiet) -> None:
    db = _db()
    await _run(monkeypatch, db, None)
    assert db.videos.docs[0]["last_failure"]["reason"].startswith("Channel authentication expired")
