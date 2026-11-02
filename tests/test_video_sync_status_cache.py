from __future__ import annotations

import asyncio
from typing import Any

from app.services import video_service
from app.services.video_service import VideoService


def _service(calls: list[str]) -> VideoService:
    service = VideoService.__new__(VideoService)

    async def compute(channel_id: str) -> dict[str, Any]:
        calls.append(channel_id)
        return {"available": True, "in_database": len(calls)}

    service._compute_sync_status = compute  # type: ignore[method-assign]
    return service


def test_status_is_reused_within_ttl_and_refreshed_after() -> None:
    video_service._sync_status_cache.clear()
    calls: list[str] = []
    service = _service(calls)

    first = asyncio.run(service.get_sync_status("ch"))
    again = asyncio.run(service.get_sync_status("ch"))
    fresh = asyncio.run(service.get_sync_status("ch", max_age=0))

    assert again == first
    assert calls == ["ch", "ch"]
    assert fresh["in_database"] == 2


def test_unavailable_status_is_not_cached() -> None:
    video_service._sync_status_cache.clear()
    service = VideoService.__new__(VideoService)
    calls: list[int] = []

    async def compute(channel_id: str) -> dict[str, Any]:
        calls.append(1)
        return {"available": False, "reason": "Failed to reach Instagram API"}

    service._compute_sync_status = compute  # type: ignore[method-assign]
    asyncio.run(service.get_sync_status("ch"))
    asyncio.run(service.get_sync_status("ch"))
    assert len(calls) == 2


def test_cached_sync_status_never_calls_the_platform() -> None:
    video_service._sync_status_cache.clear()
    assert VideoService.cached_sync_status("nobody")["available"] is False
