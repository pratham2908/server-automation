"""Instagram demographics, story insights and the extra account-day metrics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import instagram_audience_sync as sync
from app.services.instagram_account_insights import parse_account_day
from app.services.instagram_audience import (
    audience_is_stale,
    parse_demographics,
    parse_metric_values,
    parse_story_navigation,
    story_is_live,
    story_metrics_for,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _breakdown_body(metric: str, key: str, rows: list[tuple[str, int]]) -> dict[str, Any]:
    return {
        "data": [
            {
                "name": metric,
                "total_value": {
                    "breakdowns": [
                        {"dimension_keys": [key], "results": [{"dimension_values": [v], "value": n} for v, n in rows]}
                    ]
                },
            }
        ]
    }


# ---- pure parsing ----


def test_demographics_are_sorted_largest_first():
    body = _breakdown_body("reached_audience_demographics", "age", [("13-17", 6), ("18-24", 1097), ("25-34", 400)])
    assert parse_demographics(body) == [
        {"value": "18-24", "count": 1097},
        {"value": "25-34", "count": 400},
        {"value": "13-17", "count": 6},
    ]


def test_unreported_demographics_are_an_empty_list():
    # follower_demographics currently answers with a breakdown but no results.
    body = {"data": [{"name": "follower_demographics", "total_value": {"breakdowns": [{"dimension_keys": ["age"]}]}}]}
    assert parse_demographics(body) == []
    assert parse_demographics(None) == []


def test_audience_refreshes_at_most_once_a_day():
    assert audience_is_stale(None, NOW)
    assert not audience_is_stale(NOW - timedelta(hours=5), NOW)
    assert audience_is_stale(NOW - timedelta(hours=21), NOW)
    # Mongo hands back naive UTC.
    assert not audience_is_stale((NOW - timedelta(hours=5)).replace(tzinfo=None), NOW)


def test_story_navigation_is_keyed_by_action():
    body = _breakdown_body("navigation", "story_navigation_action_type", [("TAP_FORWARD", 24), ("tap_exit", 6)])
    assert parse_story_navigation(body) == {"tap_forward": 24, "tap_exit": 6}


def test_story_is_live_for_24_hours():
    assert story_is_live(NOW - timedelta(hours=23), NOW)
    assert not story_is_live(NOW - timedelta(hours=25), NOW)
    assert not story_is_live(None, NOW)


def test_instagram_login_does_not_ask_for_link_clicks():
    # It fails the whole request on the Instagram login (probed live).
    assert "link_clicks" not in story_metrics_for("instagram")
    assert "link_clicks" in story_metrics_for("facebook")


def test_metric_values_read_both_value_shapes():
    body = {"data": [{"name": "views", "values": [{"value": 50}]}, {"name": "reach", "total_value": {"value": 38}}]}
    assert parse_metric_values(body) == {"views": 50, "reach": 38}


def test_account_day_reads_engagement_and_views_by_follower():
    totals = {
        "data": [
            {"name": n, "total_value": {"value": v}}
            for n, v in (
                ("likes", 492),
                ("comments", 12),
                ("shares", 77),
                ("saves", 201),
                ("reposts", 8),
                ("replies", 0),
            )
        ]
    }
    views_by_follow = _breakdown_body("views", "follow_type", [("FOLLOWER", 594), ("NON_FOLLOWER", 50334)])
    day = parse_account_day("2026-10-15", totals, None, None, None, views_by_follow)
    assert (day.likes, day.comments, day.shares, day.saves, day.reposts, day.replies) == (492, 12, 77, 201, 8, 0)
    assert (day.views_followers, day.views_non_followers) == (594, 50334)


# ---- sync shell ----


class _Coll:
    def __init__(self, docs: list[dict[str, Any]] | None = None):
        self.docs = docs or []
        self.upserts: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def find_one(self, query: dict[str, Any], sort: Any = None) -> dict[str, Any] | None:
        rows = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        return rows[-1] if rows else None

    async def update_one(self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False) -> None:
        self.upserts.append((query, update["$set"]))


@pytest.mark.asyncio
async def test_fresh_audience_is_not_fetched_again(monkeypatch):
    monkeypatch.setattr(sync, "now_ist", lambda: NOW)
    db = SimpleNamespace(instagram_audience=_Coll([{"channel_id": "c", "fetched_at": NOW - timedelta(hours=2)}]))
    calls: list[tuple[str, ...]] = []
    ig = SimpleNamespace(get_demographics=lambda *a: calls.append(a))
    await sync.refresh_audience(db, ig, "c", "ig1")
    assert calls == []


@pytest.mark.asyncio
async def test_stale_audience_fetches_every_breakdown_and_stores_one_doc(monkeypatch):
    monkeypatch.setattr(sync, "now_ist", lambda: NOW)
    db = SimpleNamespace(instagram_audience=_Coll())
    calls: list[tuple[str, ...]] = []

    def demographics(ig_user_id: str, metric: str, breakdown: str, timeframe: str) -> dict[str, Any] | None:
        calls.append((metric, breakdown, timeframe))
        if metric == "follower_demographics":
            return None  # refused: stored as an empty list, not an error
        return _breakdown_body(metric, breakdown, [("x", 1)])

    await sync.refresh_audience(db, SimpleNamespace(get_demographics=demographics), "c", "ig1")
    # reached and engaged across 4 timeframes, plus followers once (their timeframe changes nothing), 4 breakdowns each.
    assert len(calls) == (2 * 4 + 1) * 4
    (query, stored) = db.instagram_audience.upserts[0]
    assert query == {"channel_id": "c", "date": "2026-10-16"}
    assert stored["audiences"]["reached"]["this_month"]["city"] == [{"value": "x", "count": 1}]
    assert stored["audiences"]["followers"]["this_week"]["age"] == []


@pytest.mark.asyncio
async def test_a_fresh_story_keeps_its_last_numbers_when_instagram_withholds_them(monkeypatch):
    import requests

    db = SimpleNamespace(instagram_stories=_Coll())

    def refuse(story_id: str, metrics: tuple[str, ...]) -> dict[str, Any]:
        resp = SimpleNamespace(
            json=lambda: {"error": {"message": "(#10) Not enough viewers for the media to show insights"}}
        )
        raise requests.HTTPError(response=resp)  # type: ignore[arg-type]

    ig = SimpleNamespace(
        provider="instagram",
        get_live_stories=lambda ig_user_id: [
            {"id": "s1", "media_type": "VIDEO", "timestamp": "2026-10-16T11:32:09+0000"}
        ],
        get_story_insights=refuse,
        get_story_navigation=lambda story_id: None,
    )
    await sync.refresh_stories(db, ig, "c", "ig1")
    (_, stored) = db.instagram_stories.upserts[0]
    assert "metrics" not in stored  # $set leaves the earlier reading in place
    assert "Not enough viewers" in stored["insights_note"]
    assert stored["posted_at"] == datetime(2026, 10, 16, 11, 32, 9, tzinfo=timezone.utc)


# --- timeframes and how fresh Instagram's own numbers are ---------------------


class _FakeDemographics:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def get_demographics(self, _uid: str, metric: str, breakdown: str, timeframe: str) -> dict[str, Any]:
        self.calls.append((metric, breakdown, timeframe))
        return _breakdown_body(metric, breakdown, [("x", 5)])


class _AudienceCollection:
    def __init__(self, latest: dict[str, Any] | None) -> None:
        self.latest = latest
        self.saved: dict[str, Any] = {}

    async def find_one(self, *_a: Any, **_k: Any) -> dict[str, Any] | None:
        return self.latest

    async def update_one(self, _flt: Any, update: dict[str, Any], **_k: Any) -> None:
        self.saved = update["$set"]


def _refresh(latest: dict[str, Any] | None = None) -> tuple[_FakeDemographics, dict[str, Any]]:
    ig = _FakeDemographics()
    coll = _AudienceCollection(latest)
    db = SimpleNamespace(instagram_audience=coll)
    import asyncio

    asyncio.run(sync.refresh_audience(db, ig, "c1", "u1"))  # type: ignore[arg-type]
    return ig, coll.saved


def test_reached_and_engaged_are_asked_for_every_timeframe():
    ig, saved = _refresh()
    for kind in ("reached", "engaged"):
        assert set(saved["audiences"][kind]) == {"this_week", "this_month", "last_14_days", "last_90_days"}


def test_followers_are_asked_once_because_the_timeframe_changes_nothing():
    ig, saved = _refresh()
    follower_calls = [c for c in ig.calls if c[0] == "follower_demographics"]
    assert len(follower_calls) == 4  # one per breakdown, not one per breakdown and timeframe
    assert saved["audiences"]["followers"]["this_week"] == saved["audiences"]["followers"]["last_90_days"]


def test_a_refresh_costs_36_calls():
    ig, _ = _refresh()
    assert len(ig.calls) == 36


def test_first_pull_dates_the_figures_to_now():
    _, saved = _refresh()
    assert saved["data_changed_at"] == saved["fetched_at"]


def test_unchanged_figures_keep_the_time_they_were_first_seen():
    first = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
    _, saved = _refresh()
    previous = {
        "fetched_at": first,
        "data_changed_at": first,
        "audiences": saved["audiences"],
    }
    _, again = _refresh(previous)
    assert again["data_changed_at"] == first


def test_changed_figures_are_dated_to_the_pull_that_saw_the_change():
    first = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
    previous = {"fetched_at": first, "data_changed_at": first, "audiences": {"reached": {}}}
    _, saved = _refresh(previous)
    assert saved["data_changed_at"] == saved["fetched_at"] != first


@pytest.mark.asyncio
async def test_a_forced_refresh_pulls_even_a_fresh_audience(monkeypatch):
    monkeypatch.setattr(sync, "now_ist", lambda: NOW)
    db = SimpleNamespace(instagram_audience=_Coll([{"channel_id": "c", "fetched_at": NOW - timedelta(minutes=30)}]))
    ig = SimpleNamespace(get_demographics=lambda _u, m, b, _t: _breakdown_body(m, b, [("x", 1)]))
    assert await sync.refresh_audience(db, ig, "c", "ig1", force=True) is True
    assert len(db.instagram_audience.upserts) == 1


@pytest.mark.asyncio
async def test_a_pull_instagram_refuses_entirely_keeps_the_last_snapshot(monkeypatch):
    monkeypatch.setattr(sync, "now_ist", lambda: NOW)
    db = SimpleNamespace(instagram_audience=_Coll([{"channel_id": "c", "fetched_at": NOW - timedelta(days=2)}]))
    ig = SimpleNamespace(get_demographics=lambda *_a: None)
    assert await sync.refresh_audience(db, ig, "c", "ig1", force=True) is False
    assert db.instagram_audience.upserts == []


def _router_db(latest: dict[str, Any] | None) -> SimpleNamespace:
    class _Audience:
        def __init__(self) -> None:
            self.doc = latest

        async def find_one(self, *_a: Any, **_k: Any) -> dict[str, Any] | None:
            return self.doc

        async def update_one(self, _q: Any, update: dict[str, Any], **_k: Any) -> None:
            self.doc = {**(self.doc or {}), **update["$set"]}

    return SimpleNamespace(instagram_audience=_Audience())


@pytest.mark.asyncio
async def test_refetch_pulls_now_and_returns_the_new_snapshot(monkeypatch):
    from app.routers import instagram_insights as router

    calls: list[str] = []

    def demographics(_u: str, metric: str, breakdown: str, _t: str) -> dict[str, Any]:
        calls.append(metric)
        return _breakdown_body(metric, breakdown, [("IN", 5)])

    class _Manager:
        async def get_service_and_user_id(self, channel_id: str):
            return SimpleNamespace(get_demographics=demographics), "ig1"

    monkeypatch.setattr("app.main.instagram_service_manager", _Manager(), raising=False)
    monkeypatch.setattr(sync, "now_ist", lambda: NOW)
    monkeypatch.setattr(router, "now_ist", lambda: NOW)
    db = _router_db({"channel_id": "c", "fetched_at": NOW - timedelta(hours=3), "audiences": {}})
    out = await router.refetch_audience("c", db)
    assert calls
    assert out.fetched_at is not None and out.audiences["reached"]["this_week"]["country"][0].count == 5


@pytest.mark.asyncio
async def test_refetch_right_after_a_pull_reuses_it(monkeypatch):
    from app.routers import instagram_insights as router

    class _Manager:
        async def get_service_and_user_id(self, channel_id: str):
            raise AssertionError("no Instagram call inside the cooldown")

    monkeypatch.setattr("app.main.instagram_service_manager", _Manager(), raising=False)
    monkeypatch.setattr(router, "now_ist", lambda: NOW)
    db = _router_db({"channel_id": "c", "fetched_at": NOW - timedelta(seconds=30), "audiences": {}})
    out = await router.refetch_audience("c", db)
    assert out.fetched_at is not None


@pytest.mark.asyncio
async def test_refetch_reports_when_instagram_answers_nothing(monkeypatch):
    from fastapi import HTTPException

    from app.routers import instagram_insights as router

    class _Manager:
        async def get_service_and_user_id(self, channel_id: str):
            return SimpleNamespace(get_demographics=lambda *_a: None), "ig1"

    monkeypatch.setattr("app.main.instagram_service_manager", _Manager(), raising=False)
    monkeypatch.setattr(router, "now_ist", lambda: NOW)
    db = _router_db(None)
    with pytest.raises(HTTPException) as err:
        await router.refetch_audience("c", db)
    assert err.value.status_code == 502
    assert router._refetching == set()
