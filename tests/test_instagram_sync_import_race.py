"""The Instagram sync never imports a second record for a reel our own publish worker stored mid-sync.

2026-11-21: the sync read the known media ids, then swept Instagram. The publish worker stored reel 924's media
id in between, so the sweep imported it again as a new, duplicate record.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services import video_service as vs_mod
from app.services.video_service import VideoService
from tests.mongo_fakes import DB

CHANNEL = "zara kapoor"


def _reel(mid: str) -> dict[str, Any]:
    return {
        "id": mid,
        "caption": f"reel {mid}",
        "timestamp": "2026-11-21T08:00:00+0000",
        "permalink": f"https://ig/{mid}",
    }


class _Ig:
    def __init__(self, reels: list[dict[str, Any]]) -> None:
        self.reels = reels

    def get_media_sweep(self, user_id: str) -> SimpleNamespace:
        return SimpleNamespace(reels=self.reels, insights={r["id"]: {"views": 5} for r in self.reels}, posts=[])

    def get_reel_insights(self, ids: list[str]) -> dict[str, Any]:
        return {}


def _service(
    monkeypatch: pytest.MonkeyPatch, db: DB, reels: list[dict[str, Any]], store_during_ai: list[str]
) -> VideoService:
    svc = VideoService(db=db)

    async def ig_service(channel_id: str) -> _Ig:
        return _Ig(reels)

    async def nothing(*_: Any, **__: Any) -> Any:
        return []

    async def categorize(schema: Any, cats: Any, batch: list[dict[str, Any]], *_: Any) -> list[dict[str, Any]]:
        # While the AI works, the publish worker stores its own record for these reels.
        for mid in store_during_ai:
            await db.videos.insert_one({"channel_id": CHANNEL, "video_id": f"ours-{mid}", "instagram_media_id": mid})
        return [{"instagram_media_id": v["instagram_media_id"], "category": "Reels"} for v in batch]

    monkeypatch.setattr(svc, "_get_instagram_service", ig_service)
    monkeypatch.setattr(svc, "_archive_missing_instagram_videos", nothing)
    monkeypatch.setattr(svc, "_refresh_instagram_account_insights", nothing)
    monkeypatch.setattr(svc, "_extract_params_and_categorize_batch", categorize)
    monkeypatch.setattr(vs_mod, "record_channel_totals", nothing)
    return svc


def _records(db: DB, mid: str) -> list[dict[str, Any]]:
    return [d for d in db.videos.docs if d.get("instagram_media_id") == mid]


@pytest.mark.asyncio
async def test_a_reel_stored_by_the_publisher_during_the_sync_is_not_imported_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = DB(videos=[], categories=[{"channel_id": CHANNEL, "name": "Reels"}])
    svc = _service(monkeypatch, db, [_reel("924"), _reel("925")], store_during_ai=["924"])
    result = await svc._sync_instagram_videos(CHANNEL, {"instagram_user_id": "1"}, None)
    # 924: only the publisher's own record. 925: a genuinely new reel, imported once.
    assert [d["video_id"] for d in _records(db, "924")] == ["ours-924"]
    assert len(_records(db, "925")) == 1
    assert result["synced"] == 1


@pytest.mark.asyncio
async def test_a_reel_stored_after_the_snapshot_but_before_the_import_is_skipped_without_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = DB(videos=[], categories=[{"channel_id": CHANNEL, "name": "Reels"}])
    svc = _service(monkeypatch, db, [_reel("924")], store_during_ai=[])
    asked: list[str] = []
    real_find = db.videos.find

    def find(query: dict[str, Any] | None = None, projection: Any = None):
        # The publisher stores 924 right after the sync's first read (the snapshot of known ids).
        if query and "$in" in str(query.get("instagram_media_id")) and not _records(db, "924"):
            db.videos.docs.append({"channel_id": CHANNEL, "video_id": "ours-924", "instagram_media_id": "924"})
        return real_find(query, projection)

    async def categorize(schema: Any, cats: Any, batch: list[dict[str, Any]], *_: Any) -> list[dict[str, Any]]:
        asked.extend(v["instagram_media_id"] for v in batch)
        return []

    monkeypatch.setattr(db.videos, "find", find)
    monkeypatch.setattr(svc, "_extract_params_and_categorize_batch", categorize)
    result = await svc._sync_instagram_videos(CHANNEL, {"instagram_user_id": "1"}, None)
    assert [d["video_id"] for d in _records(db, "924")] == ["ours-924"]
    assert asked == [] and result["synced"] == 0
