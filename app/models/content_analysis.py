"""Response shapes shared by the Instagram and YouTube per-video analysis endpoints.

Both platforms answer in the same shapes so the dashboard draws them with the
same charts; a field a platform can't report is simply ``None``.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, Field

from app.services.instagram_content_analysis import GroupStat, PostingSlot


class ReelDayOut(BaseModel):
    date: str
    views: float
    follows: float | None = None
    profile_visits: float | None = None
    bio_link_taps: float | None = None
    rough: bool
    # "pending": Instagram hasn't reported that day's follows yet (last ~2 days).
    # "unavailable": it never will — older than the stored account history.
    # "measured": YouTube's own per-video count (``follows`` is subscribers gained).
    status: Literal["estimated", "pending", "unavailable", "measured"]
    # YouTube only.
    subscribers_lost: float | None = None
    minutes_watched: float | None = None
    swiped_away_rate: float | None = None


class ReelTimelineOut(BaseModel):
    media_id: str
    days: list[ReelDayOut] = []


class ReelHourOut(BaseModel):
    # Start of the IST clock hour.
    start: str
    # Gains in that hour; None where a long gap between readings can't place them.
    views: float | None = None
    likes: float | None = None
    comments: float | None = None
    shares: float | None = None
    saves: float | None = None
    # Where these stood at the last reading in the hour (levels, not gains); None if no reading fell in it.
    skip_rate: float | None = None
    avg_watch_seconds: float | None = None
    watch_through: float | None = None


class ReelHourlyOut(BaseModel):
    media_id: str
    max_gap_hours: float
    hours: list[ReelHourOut] = []


class ReelAnalysisOut(BaseModel):
    media_id: str
    video_id: str | None = None
    title: str | None = None
    thumbnail_url: str | None = None
    category: str | None = None
    published_at: str | None = None
    views: float | None = None
    skip_rate: float | None = None
    avg_watch_seconds: float | None = None
    duration_seconds: float | None = None
    watch_through: float | None = None
    shares_per_1k: float | None = None
    saves_per_1k: float | None = None
    likes_per_1k: float | None = None
    comments_per_1k: float | None = None
    # Lifetime counts, for charting one against another.
    reach: float | None = None
    likes: float | None = None
    comments: float | None = None
    shares: float | None = None
    saves: float | None = None
    watch_hours: float | None = None
    # Plays per reached account; above 1 means people replayed it.
    views_per_reach: float | None = None
    # Interactions per reached account, as a percentage.
    engagement_rate: float | None = None
    estimated_follows: float | None = None
    # YouTube only: real subscribers gained from this video, and views that stayed past the opening.
    subscribers_gained: float | None = None
    engaged_views: float | None = None
    follow_quality: str | None = None
    views_24h: float | None = None
    views_48h: float | None = None
    views_7d: float | None = None
    hours_to_half_views: float | None = None
    hours_to_90_views: float | None = None
    picking_up: bool | None = None
    # The owner's property tags: {property_id: [value_id, ...]}.
    properties: dict[str, list[str]] = Field(default_factory=dict)


class PostingSlotOut(BaseModel):
    slot: int
    reels: int
    measured: int
    median_views_24h: float | None = None
    mature: int
    median_views: float | None = None


class GroupStatOut(BaseModel):
    dimension: str
    value: str
    reels: int
    median_views: float | None = None
    median_skip_rate: float | None = None
    median_watch_through: float | None = None
    median_shares_per_1k: float | None = None
    median_saves_per_1k: float | None = None


class ContentAnalysisOut(BaseModel):
    channel_id: str
    reels: list[ReelAnalysisOut] = []
    # IST hour (0–23) and weekday (0 = Monday) each reel was posted.
    posting_hours: list[PostingSlotOut] = []
    posting_weekdays: list[PostingSlotOut] = []
    groups: list[GroupStatOut] = []


def number_or_none(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def slot_out(slot: PostingSlot) -> PostingSlotOut:
    return PostingSlotOut(**asdict(slot))


def group_out(group: GroupStat) -> GroupStatOut:
    return GroupStatOut(**asdict(group))
