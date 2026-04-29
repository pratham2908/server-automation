"""Category Pydantic models."""

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.timezone import now_ist


def _new_cat_id() -> str:
    return str(uuid.uuid4())


class CategoryStatus(str, Enum):
    """Lifecycle states for a category."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class CategoryMetadata(BaseModel):
    """Aggregated performance metrics across all published videos in a category."""

    total_videos: int = 0
    avg_views: float | None = None
    avg_likes: float | None = None
    avg_comments: float | None = None
    avg_duration_seconds: float | None = None
    avg_engagement_rate: float | None = None
    avg_like_rate: float | None = None
    avg_comment_rate: float | None = None
    avg_percentage_viewed: float | None = None
    avg_view_duration_seconds: float | None = None
    total_views: int | None = None
    total_estimated_minutes_watched: float | None = None
    avg_subscribers: float | None = None


class Category(BaseModel):
    """Full category document in the ``categories`` collection."""

    id: str = Field(default_factory=_new_cat_id, description="Unique category identifier (UUID)")
    channel_id: str
    name: str
    description: str = ""
    raw_description: str = ""
    score: float = 0.0
    status: CategoryStatus = CategoryStatus.ACTIVE
    video_count: int = 0
    video_ids: list[str] = Field(default_factory=list, description="Video IDs of eligible videos in this category")
    metadata: CategoryMetadata = Field(default_factory=CategoryMetadata)
    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)


class CategoryCreate(BaseModel):
    """Payload for adding new categories."""

    name: str
    description: str = ""
    raw_description: str = ""
    score: float = 0.0


class CategoryUpdate(BaseModel):
    """Partial update payload for an existing category."""

    name: str | None = None
    description: str | None = None
    raw_description: str | None = None
    score: float | None = None
    status: CategoryStatus | None = None
