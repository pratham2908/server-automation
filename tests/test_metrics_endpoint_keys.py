"""Endpoint stats are keyed by route template, so ids and scanner probes don't each add an entry."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware import StructuredLoggingMiddleware
from app.services.metrics import UNMATCHED_ROUTE, metrics_service


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.add_middleware(StructuredLoggingMiddleware)

    @app.get("/api/v1/channels/{channel_id}/videos/{video_id}")
    async def video(channel_id: str, video_id: str) -> dict[str, str]:
        return {"id": video_id}

    return TestClient(app)


def test_requests_for_different_ids_share_one_entry(client: TestClient) -> None:
    metrics_service.endpoint_stats.clear()
    client.get("/api/v1/channels/TryAlgoViz/videos/a1")
    client.get("/api/v1/channels/TryAlgoViz/videos/b2")
    client.get("/api/v1/channels/zara kapoor/videos/c3")
    assert metrics_service.endpoint_stats.keys() == {"GET /api/v1/channels/{channel_id}/videos/{video_id}"}
    assert metrics_service.endpoint_stats["GET /api/v1/channels/{channel_id}/videos/{video_id}"]["count"] == 3


def test_unmatched_paths_share_one_bucket(client: TestClient) -> None:
    metrics_service.endpoint_stats.clear()
    client.get("/.env.default")
    client.get("/.vscode/settings.json")
    assert metrics_service.endpoint_stats.keys() == {f"GET {UNMATCHED_ROUTE}"}
    assert metrics_service.endpoint_stats[f"GET {UNMATCHED_ROUTE}"]["errors"] == 2
