"""Threads sync: posts and their numbers land on videos, readings and the channel total."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import requests

from app.services.threads_sync import channel_totals, sync_threads_channel
from tests.mongo_fakes import DB

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


class FakeThreads:
    def __init__(
        self, posts: list[dict[str, Any]], insights: dict[str, dict[str, Any]], refuse: set[str] = frozenset()
    ) -> None:
        self.posts = posts
        self.insights = insights
        self.refuse = refuse

    def list_posts(self) -> list[dict[str, Any]]:
        return self.posts

    def post_insights(self, media_id: str) -> dict[str, Any]:
        if media_id in self.refuse:
            raise requests.HTTPError("400")
        return {"data": [{"name": k, "values": [{"value": v}]} for k, v in self.insights[media_id].items()]}

    def account_insights(self, metrics: str, since: int | None = None, until: int | None = None) -> dict[str, Any]:
        if metrics == "views":
            return {"data": [{"name": "views", "values": [{"value": 40, "end_time": "2026-10-23T07:00:00+0000"}]}]}
        return {"data": [{"name": "followers_count", "total_value": {"value": 67}}]}

    def follower_demographics(self, breakdown: str) -> dict[str, Any]:
        raise requests.HTTPError("needs 100 followers")


POSTS = [
    {
        "id": "p_new",
        "text": "Why Gibraltar is British",
        "timestamp": "2026-10-23T02:10:30+0000",
        "permalink": "https://t/p1",
        "media_type": "VIDEO",
    },
    {
        "id": "p_old",
        "text": "Old post",
        "timestamp": "2026-09-01T10:00:00+0000",
        "permalink": "https://t/p2",
        "media_type": "TEXT_POST",
    },
]
INSIGHTS = {
    "p_new": {"views": 500, "likes": 20, "replies": 3, "reposts": 1, "quotes": 0, "shares": 2},
    "p_old": {"views": 1000, "likes": 50, "replies": 5, "reposts": 0, "quotes": 1, "shares": 0},
}


def test_channel_total_needs_every_post() -> None:
    insights = {"a": {"views": 5.0, "replies": 2.0}, "b": {"views": 7.0}}
    assert channel_totals(["a", "b"], insights)["views"] == 12
    assert channel_totals(["a", "b"], insights)["comments"] == 2
    assert channel_totals(["a", "b", "c"], insights) is None


@pytest.mark.asyncio
async def test_new_posts_are_recorded_with_their_numbers() -> None:
    db = DB()
    out = await sync_threads_channel(db, FakeThreads(POSTS, INSIGHTS), "wtm-th", NOW)

    assert out["synced"] == 2 and out["followers_count"] == 67
    video = next(v for v in db.videos.docs if v["threads_media_id"] == "p_new")
    assert video["status"] == "published"
    assert video["metadata"] == {"views": 500, "likes": 20, "comments": 3, "reposts": 1, "quotes": 0, "shares": 2}
    assert video["threads_permalink"] == "https://t/p1"
    assert len(db.threads_post_snapshots.docs) == 2
    total = db.channel_view_snapshots.docs[0]
    assert (total["platform"], total["views"], total["posts"]) == ("threads", 1500, 2)
    assert db.threads_account_daily.docs[0]["date"] == "2026-10-22"


@pytest.mark.asyncio
async def test_an_incremental_sync_keeps_an_old_posts_reading_once_a_day() -> None:
    db = DB(
        videos=[
            {
                "channel_id": "wtm-th",
                "video_id": "a",
                "threads_media_id": "p_new",
                "status": "published",
                "published_at": NOW - timedelta(hours=10),
                "insights_synced_at": NOW - timedelta(hours=1),
            },
            {
                "channel_id": "wtm-th",
                "video_id": "b",
                "threads_media_id": "p_old",
                "status": "published",
                "published_at": NOW - timedelta(days=50),
                "insights_synced_at": NOW - timedelta(hours=1),
            },
        ]
    )
    await sync_threads_channel(db, FakeThreads(POSTS, INSIGHTS), "wtm-th", NOW, incremental=True)

    assert [s["media_id"] for s in db.threads_post_snapshots.docs] == ["p_new"]
    # Numbers on the video are refreshed every sync even when no reading is kept.
    assert next(v for v in db.videos.docs if v["video_id"] == "b")["metadata"]["views"] == 1000
    # Every post was read, so the channel total is still written.
    assert db.channel_view_snapshots.docs[0]["views"] == 1500


@pytest.mark.asyncio
async def test_a_post_that_refuses_insights_keeps_the_total_out() -> None:
    db = DB()
    await sync_threads_channel(db, FakeThreads(POSTS, INSIGHTS, refuse={"p_old"}), "wtm-th", NOW)
    assert db.channel_view_snapshots.docs == []
    assert len(db.videos.docs) == 2
