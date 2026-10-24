"""Verifying a video saves the owner's corrections instead of only flipping the status."""

from __future__ import annotations

import pytest

from app.services import video_service as vs
from tests.mongo_fakes import DB


@pytest.fixture
def service(monkeypatch):
    recounted: list[str] = []

    async def fake_recompute(channel_id, name, db):
        recounted.append(name)

    monkeypatch.setattr(vs, "recompute_category", fake_recompute)
    svc = vs.VideoService.__new__(vs.VideoService)
    svc.db = DB(videos=[{"channel_id": "c1", "video_id": "v1", "category": "Old", "content_params": {"topic": "ai"}}])
    return svc, recounted


@pytest.mark.asyncio
async def test_corrections_are_saved_and_both_categories_recounted(service):
    svc, recounted = service
    await svc.verify_params("c1", "v1", {"topic": "graphs"}, "New")
    doc = svc.db.videos.docs[0]
    assert (doc["content_params"], doc["category"], doc["verification_status"]) == (
        {"topic": "graphs"},
        "New",
        "verified",
    )
    assert recounted == ["Old", "New"]


@pytest.mark.asyncio
async def test_no_body_only_verifies(service):
    svc, recounted = service
    await svc.verify_params("c1", "v1", None, None)
    doc = svc.db.videos.docs[0]
    assert (doc["content_params"], doc["category"], doc["verification_status"]) == ({"topic": "ai"}, "Old", "verified")
    assert recounted == []


@pytest.mark.asyncio
async def test_unknown_video_raises(service):
    svc, _ = service
    with pytest.raises(ValueError):
        await svc.verify_params("c1", "nope", None, None)
