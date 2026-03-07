"""Category Pydantic models."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class CategoryStatus(str, Enum):
    """Lifecycle states for a category."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class CategoryMetadata(BaseModel):
    """Aggregated performance metrics across all published videos in a category."""

    total_videos: int = 0
    avg_views: Optional[float] = None
    avg_likes: Optional[float] = None
    avg_comments: Optional[float] = None
    avg_duration_seconds: Optional[float] = None
    avg_engagement_rate: Optional[float] = None
    avg_like_rate: Optional[float] = None
    avg_comment_rate: Optional[float] = None
    avg_percentage_viewed: Optional[float] = None
    avg_view_duration_seconds: Optional[float] = None
    total_views: Optional[int] = None
    total_estimated_hours_watched: Optional[float] = None


class Category(BaseModel):
    """Full category document in the ``categories`` collection."""

    channel_id: str
    name: str
    description: str = ""
    raw_description: str = ""
    score: float = 0.0
    status: CategoryStatus = CategoryStatus.ACTIVE
    video_count: int = 0
    metadata: CategoryMetadata = Field(default_factory=CategoryMetadata)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CategoryCreate(BaseModel):
    """Payload for adding new categories."""

    name: str
    description: str = ""
    raw_description: str = ""
    score: float = 0.0


class CategoryUpdate(BaseModel):
    """Partial update payload for an existing category."""

    name: Optional[str] = None
    description: Optional[str] = None
    raw_description: Optional[str] = None
    score: Optional[float] = None
    status: Optional[CategoryStatus] = None
