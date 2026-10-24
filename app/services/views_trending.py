"""Which videos gained the most views lately — the Reels tab's "Trending" quick filter.

Unlike the hourly top-videos list, this works for old videos too: a video past
its first week is read only once a day, so its gain can't be placed hour by hour,
but the views it added over the last day still can be — the latest reading minus
the views it had a day before that, interpolated between the readings either side.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.database import get_channel_platform
from app.services.instagram_content_analysis import parse_published_at, views_at
from app.services.instagram_follow_attribution import ViewReading
from app.services.views_top_videos import MEDIA_FIELDS, view_readings_by_media
from app.timezone import IST, now_ist

# Old videos are read about once a day, so the readings around the window's start can be
# a day apart; any wider and the interpolated starting count is a guess.
TRENDING_MAX_GAP_HOURS = 30.0


class TrendingVideo(BaseModel):
    platform_id: str
    views_gained: float
    # The newest reading the gain runs up to; a video read once a day can be most of a day behind.
    as_of: str


class TrendingVideosOut(BaseModel):
    channel_id: str
    hours: int
    videos: list[TrendingVideo]


def recent_gain(published_at: datetime | None, readings: Sequence[ViewReading], hours: float) -> float | None:
    """Views gained in the *hours* up to the newest reading, or None when the readings can't say."""
    ordered = sorted(readings, key=lambda r: r.at)
    if not ordered:
        return None
    last = ordered[-1]
    start = last.at - timedelta(hours=hours)
    if published_at is not None and published_at >= start:
        # Posted inside the window: everything it has came in it.
        return max(0.0, last.views)
    before = views_at(ordered, start, TRENDING_MAX_GAP_HOURS)
    if before is None:
        return None
    # A platform revising a count down is no negative trend.
    return max(0.0, last.views - before)


async def get_trending_videos(
    db: AsyncIOMotorDatabase, channel: dict[str, Any], hours: int, limit: int
) -> TrendingVideosOut:
    channel_id = channel["channel_id"]
    platform = get_channel_platform(channel)
    # Enough history to find the reading before the window's start for a video last read a day ago.
    since = now_ist() - timedelta(hours=2 * hours + 48)
    readings = await view_readings_by_media(db, channel_id, platform, since)

    media_field = MEDIA_FIELDS.get(platform, "youtube_video_id")
    published: dict[str, datetime | None] = {}
    async for video in db.videos.find(
        {"channel_id": channel_id, media_field: {"$in": list(readings)}}, {media_field: 1, "published_at": 1}
    ):
        published[str(video[media_field])] = parse_published_at(video.get("published_at"))

    videos: list[TrendingVideo] = []
    for mid, points in readings.items():
        gain = recent_gain(published.get(mid), [ViewReading(at=at, views=v) for at, v in points], hours)
        if gain:
            videos.append(
                TrendingVideo(
                    platform_id=mid, views_gained=round(gain, 1), as_of=points[-1][0].astimezone(IST).isoformat()
                )
            )
    videos.sort(key=lambda v: v.views_gained, reverse=True)
    return TrendingVideosOut(channel_id=channel_id, hours=hours, videos=videos[:limit])
