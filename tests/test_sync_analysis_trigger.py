"""The auto-analysis trigger counts only videos the analysis would pick up."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.services.sync_analysis_cron import _count_unanalyzed_videos
from app.timezone import now_ist


def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, cond in query.items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in cond):
                return False
        elif isinstance(cond, dict) and "$lte" in cond:
            value = doc.get(key)
            if value is None or value > cond["$lte"]:
                return False
        elif doc.get(key) != cond:
            return False
    return True


class _Videos:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs

    async def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for d in self.docs if _matches(d, query))


class _Db:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.videos = _Videos(docs)


def _video(age_days: float | None, performance: object = None, status: str = "published") -> dict[str, Any]:
    doc: dict[str, Any] = {"channel_id": "ch", "status": status, "performance": performance}
    doc["created_at"] = None if age_days is None else now_ist() - timedelta(days=age_days)
    return doc


@pytest.mark.asyncio
async def test_videos_under_three_days_do_not_count() -> None:
    # Three fresh videos used to hold a channel at the threshold forever.
    db = _Db([_video(0.5), _video(1), _video(2.9)])
    assert await _count_unanalyzed_videos(db, "ch") == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_old_unanalysed_and_undated_videos_count() -> None:
    db = _Db(
        [
            _video(4),
            _video(30),
            _video(None),
            _video(10, performance={"status": "completed"}),
            _video(10, status="scheduled"),
        ]
    )
    assert await _count_unanalyzed_videos(db, "ch") == 3  # type: ignore[arg-type]
