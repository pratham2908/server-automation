from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.timezone import now_ist


class CompetitorVideoRef(BaseModel):
    """A reference to a specific video/reel from a competitor."""

    video_id: str
    title: str
    permalink: str
    published_at: str
    views: int = 0
    likes: int = 0
    comments: int = 0
    competitor_name: str
    platform: str = "youtube"


class TopicGroup(BaseModel):
    """A cluster of videos from various competitors that share the same exact concept."""

    topic_name: str = Field(..., description="The AI-generated name for this concept")
    category: str = Field(default="Uncategorized", description="The broader category this concept belongs to")
    description: str = Field(..., description="A brief description of what makes this concept successful")
    videos: list[CompetitorVideoRef] = Field(default_factory=list)

    total_views: int = 0
    total_likes: int = 0
    competitor_count: int = 0

    # Discovery metadata
    channel_id: str = Field(..., description="The managed channel this discovery was run for")
    discovered_at: datetime = Field(default_factory=now_ist)
    recommendation_score: float = Field(default=0.0, description="0-100 score based on total performance")


class TopicDiscoveryResult(BaseModel):
    """The final result of a discovery scan."""

    channel_id: str
    topics: list[TopicGroup]
    status: str = "success"
    scanned_at: datetime = Field(default_factory=now_ist)


class DoneTopic(BaseModel):
    """A topic that has been marked as completed by the user."""

    channel_id: str
    topic_name: str
    marked_at: datetime = Field(default_factory=now_ist)
