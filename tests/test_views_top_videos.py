"""Tests for top videos by duration and candle bucket."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.services.instagram_reel_hourly import QUARTER
from app.services.views_top_videos import (
    _format_video_url,
    _parse_hour_bucket,
    get_channel_top_videos,
)
from app.timezone import IST


def test_format_video_url() -> None:
    assert _format_video_url("youtube", "abc1234") == "https://www.youtube.com/watch?v=abc1234"
    assert _format_video_url("instagram", "123456789") == "https://www.instagram.com/reel/123456789/"


def test_parse_hour_bucket() -> None:
    dt1 = _parse_hour_bucket("2026-10-22T04:30:15+05:30")
    assert dt1 == datetime(2026, 10, 22, 4, 0, tzinfo=IST)

    dt2 = _parse_hour_bucket("2026-10-22T04:00:00Z")
    # 04:00 UTC is 09:30 IST -> floor is 09:00 IST
    assert dt2 == datetime(2026, 10, 22, 9, 0, tzinfo=IST)

    assert _parse_hour_bucket("invalid-date") is None
    assert _parse_hour_bucket("2026-10-22T04:40:00+05:30", QUARTER) == datetime(2026, 10, 22, 4, 30, tzinfo=IST)


@pytest.mark.asyncio
async def test_invalid_range_raises_422() -> None:
    db = MagicMock()
    with pytest.raises(HTTPException) as exc_info:
        await get_channel_top_videos(db, "chan1", "invalid_range")
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_channel_not_found_raises_404() -> None:
    db = MagicMock()
    db.channels.find_one = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as exc_info:
        await get_channel_top_videos(db, "unknown_chan", "48h")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_daily_top_videos_youtube() -> None:
    db = MagicMock()
    db.channels.find_one = AsyncMock(return_value={"channel_id": "c1", "platform": "youtube"})
    
    # Mock aggregate cursor on youtube_video_daily
    agg_cursor = MagicMock()
    agg_cursor.to_list = AsyncMock(return_value=[
        {"_id": "yt1", "views": 250.0},
        {"_id": "yt2", "views": 100.0},
    ])
    db.youtube_video_daily.aggregate = MagicMock(return_value=agg_cursor)

    # Mock video metadata find
    video_cursor = MagicMock()
    video_cursor.to_list = AsyncMock(return_value=[
        {
            "video_id": "v-1",
            "youtube_video_id": "yt1",
            "title": "Video One",
            "thumbnail_url": "https://img/1.jpg",
            "metadata": {"views": 5000},
            "published_at": "2026-10-01T10:00:00",
        },
        {
            "video_id": "v-2",
            "youtube_video_id": "yt2",
            "title": "Video Two",
            "thumbnail_url": "https://img/2.jpg",
            "metadata": {"views": 2000},
            "published_at": "2026-10-05T10:00:00",
        },
    ])
    db.videos.find = MagicMock(return_value=video_cursor)

    res = await get_channel_top_videos(db, "c1", "28d", limit=6)
    assert res.channel_id == "c1"
    assert res.range == "28d"
    assert res.granularity == "day"
    assert res.total_views == 350.0
    assert len(res.videos) == 2
    assert res.videos[0].video_id == "v-1"
    assert res.videos[0].platform_id == "yt1"
    assert res.videos[0].views_gained == 250.0
    assert res.videos[0].percentage == 71.4
    assert res.videos[0].url == "https://www.youtube.com/watch?v=yt1"

    assert res.videos[1].video_id == "v-2"
    assert res.videos[1].views_gained == 100.0
    assert res.videos[1].percentage == 28.6


@pytest.mark.asyncio
async def test_hourly_top_videos_instagram() -> None:
    db = MagicMock()
    db.channels.find_one = AsyncMock(return_value={"channel_id": "c_ig", "platform": "instagram"})

    t0 = datetime(2026, 10, 22, 10, 0, tzinfo=IST)
    t1 = datetime(2026, 10, 22, 11, 0, tzinfo=IST)
    t2 = datetime(2026, 10, 22, 12, 0, tzinfo=IST)

    # Async cursor iterator
    class AsyncIter:
        def __init__(self, items):
            self.items = items
            self.idx = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.idx >= len(self.items):
                raise StopAsyncIteration
            item = self.items[self.idx]
            self.idx += 1
            return item

        def sort(self, *args, **kwargs):
            return self

    snaps = [
        {"media_id": "m1", "at": t0, "views": 100},
        {"media_id": "m1", "at": t1, "views": 150},
        {"media_id": "m1", "at": t2, "views": 200},
        {"media_id": "m2", "at": t0, "views": 10},
        {"media_id": "m2", "at": t2, "views": 30},
    ]
    db.instagram_reel_snapshots.find = MagicMock(return_value=AsyncIter(snaps))

    video_cursor = MagicMock()
    video_cursor.to_list = AsyncMock(return_value=[
        {
            "video_id": "vid-ig-1",
            "instagram_media_id": "m1",
            "title": "Reel 1",
            "thumbnail_url": "https://ig/1.jpg",
            "metadata": {"views": 1000},
        },
        {
            "video_id": "vid-ig-2",
            "instagram_media_id": "m2",
            "title": "Reel 2",
            "thumbnail_url": "https://ig/2.jpg",
            "metadata": {"views": 500},
        },
    ])
    db.videos.find = MagicMock(return_value=video_cursor)

    res = await get_channel_top_videos(db, "c_ig", "48h")
    assert res.channel_id == "c_ig"
    assert res.range == "48h"
    assert res.granularity == "hour"
    assert len(res.videos) == 2
    assert res.videos[0].video_id == "vid-ig-1"
    assert res.videos[0].platform_id == "m1"
    assert res.videos[0].views_gained == 100.0
    assert res.videos[0].url == "https://www.instagram.com/reel/m1/"

