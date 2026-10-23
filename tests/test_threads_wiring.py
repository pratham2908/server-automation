"""Threads plugged into scheduling, connecting, token refresh and the views chart."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import requests
from fastapi import HTTPException

from app.routers import threads as threads_router
from app.services import schedule_operation as so
from app.services import threads_token_refresher as tr
from app.services.instagram_reel_hourly import MetricReading
from app.services.instagram_tokens import secret_projection
from app.services.view_series import daily_from_readings
from app.timezone import IST
from tests.mongo_fakes import DB

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_threads_videos_are_queued_for_the_threads_publisher() -> None:
    db = DB(videos=[{"channel_id": "c", "video_id": "v", "status": "ready", "r2_object_key": "k"}])
    out = await so.schedule_on_platform(
        db=db, platform="threads", channel_id="c", video_doc=db.videos.docs[0], scheduled_at=NOW
    )
    assert out["status"] == "queued"
    assert db.schedule_queue.docs[0]["platform"] == "threads"
    assert db.videos.docs[0]["status"] == "queued"


@pytest.mark.asyncio
async def test_an_unknown_platform_is_refused_rather_than_sent_down_another_path() -> None:
    with pytest.raises(ValueError):
        await so.schedule_on_platform(
            db=DB(), platform="tiktok", channel_id="c", video_doc={"video_id": "v"}, scheduled_at=NOW
        )


def test_the_threads_token_never_leaves_in_a_channel_response() -> None:
    assert secret_projection()["threads_tokens"] == 0


@pytest.mark.asyncio
async def test_connecting_swaps_the_code_and_stores_the_account(monkeypatch) -> None:
    db = DB(
        channels=[{"channel_id": "wtm-th", "platform": "threads", "name": "wtm-th"}],
        config=[{"key": "threads_oauth", "app_id": "app", "app_secret": "secret"}],
    )
    seen: dict[str, Any] = {}

    def fake_exchange(app_id, app_secret, code, redirect_uri):
        seen["exchange"] = (app_id, code, redirect_uri)
        return {"access_token": "short", "user_id": "777"}

    class FakeService:
        def __init__(self, token: str) -> None:
            seen["token"] = token

        def get_profile(self) -> dict[str, Any]:
            return {
                "id": "777",
                "username": "why_the_map",
                "name": "Why The Map",
                "threads_profile_picture_url": "https://pic",
            }

    monkeypatch.setattr(threads_router, "exchange_code", fake_exchange)
    monkeypatch.setattr(
        threads_router, "exchange_long_lived", lambda secret, short: {"access_token": "long", "expires_in": 5184000}
    )
    monkeypatch.setattr(threads_router, "ThreadsService", FakeService)

    async def fake_persist(db_, channel_id, update):
        await db_.channels.update_one({"channel_id": channel_id}, {"$set": update})

    monkeypatch.setattr(threads_router, "persist_profile_update", fake_persist)

    out = await threads_router.connect_threads(
        "wtm-th", threads_router.ThreadsConnect(code="abc#_", redirect_uri="https://app/cb"), db
    )
    assert out["username"] == "why_the_map"
    assert seen["exchange"] == ("app", "abc#_", "https://app/cb")
    assert seen["token"] == "long"
    channel = db.channels.docs[0]
    assert channel["threads_user_id"] == "777"
    assert channel["threads_tokens"]["access_token"] == "long"
    assert channel["threads_tokens"]["expires_at"] is not None
    assert channel["threads_username"] == "why_the_map"


@pytest.mark.asyncio
async def test_one_threads_account_cannot_back_two_channels(monkeypatch) -> None:
    db = DB(
        channels=[
            {"channel_id": "a", "platform": "threads"},
            {"channel_id": "b", "platform": "threads", "threads_user_id": "777"},
        ],
        config=[{"key": "threads_oauth", "app_id": "app", "app_secret": "secret"}],
    )
    monkeypatch.setattr(threads_router, "exchange_code", lambda *a: {"access_token": "s", "user_id": "777"})
    monkeypatch.setattr(threads_router, "exchange_long_lived", lambda *a: {"access_token": "l", "expires_in": 10})

    class FakeService:
        def __init__(self, token: str) -> None:
            pass

        def get_profile(self) -> dict[str, Any]:
            return {"id": "777", "username": "why_the_map"}

    monkeypatch.setattr(threads_router, "ThreadsService", FakeService)
    with pytest.raises(HTTPException) as exc:
        await threads_router.connect_threads("a", threads_router.ThreadsConnect(code="c", redirect_uri="r"), db)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_a_week_old_token_is_refreshed_and_a_failing_one_near_expiry_warns(monkeypatch) -> None:
    mails: list[str] = []

    async def fake_send(settings, to, subject, body, html=None):
        mails.append(subject)
        return True

    async def owner(db, settings):
        return "owner@example.com"

    monkeypatch.setattr(tr, "send_email", fake_send)
    monkeypatch.setattr(tr, "resolve_owner_email", owner)

    db = DB(
        channels=[
            {
                "channel_id": "ok",
                "name": "OK",
                "threads_tokens": {"access_token": "t", "user_id": "1", "refreshed_at": NOW - timedelta(days=8)},
            }
        ]
    )
    monkeypatch.setattr(tr, "refresh_long_lived", lambda token: {"access_token": "t2", "expires_in": 5184000})
    assert await tr.refresh_channel_token(db, db.channels.docs[0], NOW) == "refreshed"
    assert db.channels.docs[0]["threads_tokens"]["access_token"] == "t2"
    assert db.channels.docs[0]["threads_tokens"]["user_id"] == "1"

    dying = {"access_token": "t", "refreshed_at": NOW - timedelta(days=55), "expires_at": NOW + timedelta(days=5)}
    db = DB(channels=[{"channel_id": "dying", "name": "Dying", "threads_tokens": dying}])

    def refuse(token):
        raise requests.HTTPError("190")

    monkeypatch.setattr(tr, "refresh_long_lived", refuse)
    assert await tr.refresh_channel_token(db, db.channels.docs[0], NOW) == "failed"
    # A second failure the same day doesn't mail again.
    await tr.refresh_channel_token(db, db.channels.docs[0], NOW + timedelta(hours=6))
    assert mails == ["Threads: reconnect Dying"]


def test_daily_views_from_readings_split_at_ist_midnight() -> None:
    day1 = datetime(2026, 10, 22, 23, 0, tzinfo=IST)
    readings = [
        MetricReading(at=day1, values={"views": 0.0}),
        MetricReading(at=day1 + timedelta(hours=2), values={"views": 20.0}),
    ]
    assert daily_from_readings(None, readings, "views") == {"2026-10-22": 10.0, "2026-10-23": 10.0}
