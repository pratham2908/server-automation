"""Durable, sequential queue for retention-analysis triggers.

Replaces the previous fire-and-forget behaviour where each API trigger spawned
an immediate concurrent task. Now triggers enqueue and a single worker drains
them one at a time.
"""

from types import SimpleNamespace

import pytest

from app.services.retention_queue import (
    claim_next_job,
    enqueue_retention_analysis,
    recover_stale_jobs,
)


class FakeQueueCollection:
    """In-memory stand-in for db.retention_analysis_queue.

    Supports the subset used by the queue: insert_one, find_one (with $in and
    sort), find_one_and_update (with sort + return_document), update_one,
    delete_one, and a count helper for assertions.
    """

    def __init__(self):
        self.docs: list[dict] = []
        self._counter = 0

    @staticmethod
    def _matches(doc: dict, query: dict) -> bool:
        for key, cond in query.items():
            if isinstance(cond, dict) and "$in" in cond:
                if doc.get(key) not in cond["$in"]:
                    return False
            elif doc.get(key) != cond:
                return False
        return True

    def _sorted(self, docs, sort):
        result = list(docs)
        for field, direction in reversed(sort or []):
            result.sort(key=lambda d: d.get(field, 0), reverse=direction < 0)
        return result

    async def insert_one(self, doc: dict):
        self._counter += 1
        doc = {**doc, "_id": f"id{self._counter}"}
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def find_one(self, query: dict, sort=None):
        matches = [d for d in self.docs if self._matches(d, query)]
        matches = self._sorted(matches, sort)
        return dict(matches[0]) if matches else None

    async def find_one_and_update(self, query: dict, update: dict, sort=None, return_document=None):
        matches = [d for d in self.docs if self._matches(d, query)]
        matches = self._sorted(matches, sort)
        if not matches:
            return None
        target = matches[0]
        target.update(update.get("$set", {}))
        return dict(target)

    async def update_one(self, query: dict, update: dict):
        for d in self.docs:
            if self._matches(d, query):
                d.update(update.get("$set", {}))
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def delete_one(self, query: dict):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._matches(d, query)]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    def find(self, query: dict, sort=None):
        # Motor's find() is synchronous and returns an awaitable cursor.
        matches = [dict(d) for d in self.docs if self._matches(d, query)]
        matches = self._sorted(matches, sort)
        return _FakeCursor(matches)


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return self._docs


class FakeVideos:
    def __init__(self, docs):
        self.docs = [dict(d) for d in docs]

    async def update_one(self, query, update):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                for key, value in update.get("$set", {}).items():
                    # Interpret dotted keys as nested, like MongoDB does.
                    parts = key.split(".")
                    target = d
                    for part in parts[:-1]:
                        target = target.setdefault(part, {})
                    target[parts[-1]] = value
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    def get(self, vid):
        return next((d for d in self.docs if d.get("video_id") == vid), None)


class FakeDB:
    def __init__(self, videos=None):
        self.retention_analysis_queue = FakeQueueCollection()
        self.videos = FakeVideos(videos or [{"channel_id": "c", "video_id": "v1"}, {"channel_id": "c", "video_id": "v2"}])


# ------------------------------------------------------------------
# enqueue
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_inserts_a_queued_job_at_the_next_position():
    db = FakeDB()

    r1 = await enqueue_retention_analysis(db, "c", "v1")
    r2 = await enqueue_retention_analysis(db, "c", "v2")

    assert r1 == {"queued": True, "already_queued": False, "position": 1}
    assert r2 == {"queued": True, "already_queued": False, "position": 2}
    assert len(db.retention_analysis_queue.docs) == 2


@pytest.mark.asyncio
async def test_enqueue_marks_the_video_as_pending_so_the_ui_shows_it_waiting():
    db = FakeDB()

    await enqueue_retention_analysis(db, "c", "v1")

    assert db.videos.get("v1")["retention"]["status"] == "pending"


@pytest.mark.asyncio
async def test_enqueue_is_deduplicated_for_a_video_already_waiting_or_running():
    db = FakeDB()

    await enqueue_retention_analysis(db, "c", "v1")
    second = await enqueue_retention_analysis(db, "c", "v1")

    assert second["already_queued"] is True
    assert second["queued"] is False
    assert len(db.retention_analysis_queue.docs) == 1


@pytest.mark.asyncio
async def test_a_completed_job_does_not_block_requeuing_the_same_video():
    db = FakeDB()
    await enqueue_retention_analysis(db, "c", "v1")
    # simulate the worker finishing and removing the job
    db.retention_analysis_queue.docs.clear()

    again = await enqueue_retention_analysis(db, "c", "v1")

    assert again["queued"] is True


# ------------------------------------------------------------------
# claim_next_job — atomic, position-ordered, sequential
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_next_returns_jobs_in_position_order_and_marks_them_analyzing():
    db = FakeDB()
    await enqueue_retention_analysis(db, "c", "v2")  # position 1
    await enqueue_retention_analysis(db, "c", "v1")  # position 2

    first = await claim_next_job(db)
    assert first["video_id"] == "v2"
    assert first["status"] == "analyzing"


@pytest.mark.asyncio
async def test_claim_next_skips_a_job_already_being_analyzed():
    """With one job in flight, the claim returns the next queued one, not the running one."""
    db = FakeDB()
    await enqueue_retention_analysis(db, "c", "v1")
    await enqueue_retention_analysis(db, "c", "v2")

    await claim_next_job(db)  # claims v1 -> analyzing
    second = await claim_next_job(db)  # should skip v1, claim v2

    assert second["video_id"] == "v2"


@pytest.mark.asyncio
async def test_claim_next_returns_none_when_nothing_is_queued():
    db = FakeDB()
    assert await claim_next_job(db) is None


# ------------------------------------------------------------------
# restart recovery
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_resets_interrupted_analyzing_jobs_back_to_queued():
    db = FakeDB()
    await enqueue_retention_analysis(db, "c", "v1")
    await claim_next_job(db)  # v1 -> analyzing, as if the process died mid-run

    recovered = await recover_stale_jobs(db)

    assert recovered == 1
    assert db.retention_analysis_queue.docs[0]["status"] == "queued"
