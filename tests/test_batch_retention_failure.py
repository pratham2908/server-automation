"""A batch item whose retention analysis fails must be marked failed, not completed."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services import batch_upload_service


class _Collection:
    """Equality-match collection with just what ``_process_batch_item`` calls."""

    def __init__(self, docs: list[dict[str, Any]]):
        self.docs = docs

    def _find(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return next((d for d in self.docs if all(d.get(k) == v for k, v in query.items())), None)

    async def find_one(self, query: dict[str, Any], projection: dict[str, Any] | None = None) -> dict[str, Any] | None:
        return self._find(query)

    async def find_one_and_update(
        self, query: dict[str, Any], update: dict[str, Any], **_: Any
    ) -> dict[str, Any] | None:
        doc = self._find(query)
        if doc is not None:
            doc.update(update["$set"])
        return doc

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> SimpleNamespace:
        doc = self._find(query)
        if doc is not None:
            doc.update(update["$set"])
        return SimpleNamespace(matched_count=int(doc is not None))


def _db() -> SimpleNamespace:
    return SimpleNamespace(
        batch_analysis_queue=_Collection(
            [
                {
                    "file_id": "f1",
                    "status": "queued",
                    "r2_key": "k",
                    "primary_channel_id": "main",
                    "channel_video_ids": [
                        {"channel_id": "main", "video_id": "v-main"},
                        {"channel_id": "sibling", "video_id": "v-sib"},
                    ],
                }
            ]
        ),
        videos=_Collection(
            [
                {"channel_id": "main", "video_id": "v-main"},
                {"channel_id": "sibling", "video_id": "v-sib"},
            ]
        ),
    )


async def _no_report(**_: Any) -> None:
    return None


def _retention_that(status: str):
    async def fake(channel_id: str, video_id: str, db: Any, *_: Any, **__: Any) -> None:
        doc = db.videos._find({"video_id": video_id})
        doc["retention"] = {"status": status, "error_message": "403 billing account closed"}

    return fake


@pytest.fixture
def r2(tmp_path) -> SimpleNamespace:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    return SimpleNamespace(download_video=lambda key: str(video))


@pytest.mark.asyncio
async def test_failed_retention_marks_item_and_siblings_failed(monkeypatch, r2):
    monkeypatch.setattr(batch_upload_service, "run_retention_analysis", _retention_that("failed"))
    reported: list[str] = []

    async def report(**kw: Any) -> None:
        reported.append(kw["feature"])

    monkeypatch.setattr(batch_upload_service, "report_error", report)
    db = _db()

    await batch_upload_service._process_batch_item("f1", db, r2, gemini=None)

    item = db.batch_analysis_queue.docs[0]
    assert item["status"] == "failed"
    assert "403 billing account closed" in item["message"]
    assert [v["packaging_status"] for v in db.videos.docs] == ["failed", "failed"]
    # run_retention_analysis already reported it; the batch must not file a duplicate.
    assert reported == []


@pytest.mark.asyncio
async def test_successful_retention_still_completes(monkeypatch, r2):
    monkeypatch.setattr(batch_upload_service, "run_retention_analysis", _retention_that("completed"))
    monkeypatch.setattr(batch_upload_service, "report_error", _no_report)
    db = _db()

    await batch_upload_service._process_batch_item("f1", db, r2, gemini=None)

    assert db.batch_analysis_queue.docs[0]["status"] == "completed"
