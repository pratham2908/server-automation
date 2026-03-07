"""Video and VideoQueue Pydantic models."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class VideoStatus(str, Enum):
    """Allowed lifecycle states for a video."""

    TODO = "todo"
    READY = "ready"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"


class VideoMetadata(BaseModel):
    """YouTube performance metrics (populated during sync / stats fetch)."""

    # Data API v3
    views: Optional[int] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    duration_seconds: Optional[int] = None
    engagement_rate: Optional[float] = None
    like_rate: Optional[float] = None
    comment_rate: Optional[float] = None
    # Analytics API v2
    avg_percentage_viewed: Optional[float] = None
    avg_view_duration_seconds: Optional[int] = None
    estimated_minutes_watched: Optional[float] = None


class Video(BaseModel):
    """Full video document stored in the ``videos`` collection."""

    channel_id: str
    video_id: str = Field(..., description="Auto-generated UUID")
    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    category: str = ""
    topic: str = ""
    status: VideoStatus = VideoStatus.TODO
    suggested: bool = False
    basis_factor: str = ""
    youtube_video_id: Optional[str] = None
    r2_object_key: Optional[str] = None
    metadata: VideoMetadata = Field(default_factory=VideoMetadata)
    scheduled_at: Optional[datetime] = Field(
        None,
        description="When the video is scheduled to go live on YouTube. Set when scheduled.",
    )
    published_at: Optional[datetime] = Field(
        None,
        description="When the video was published on YouTube. Null until published.",
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class VideoCreate(BaseModel):
    """Payload accepted when adding a new video to the queue."""

    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    category: str = ""
    topic: str = ""
    basis_factor: str = ""


class VideoStatusUpdate(BaseModel):
    """Body for the PATCH status endpoint."""

    status: VideoStatus


class PostingQueue(BaseModel):
    """An entry in the ready queue (``posting_queue`` collection).

    Videos with status ``ready`` sit here after being uploaded to R2,
    waiting to be scheduled on YouTube.
    """

    channel_id: str
    video_id: str = Field(..., description="References videos.video_id")
    position: int = Field(..., ge=1, description="1-based queue ordering")
    added_at: datetime = Field(default_factory=datetime.utcnow)


class ScheduleQueue(BaseModel):
    """An entry in the scheduled queue (``schedule_queue`` collection).

    Videos with status ``scheduled`` sit here after being uploaded to
    YouTube as private with a ``publishAt`` time, waiting for YouTube
    to auto-publish.
    """

    channel_id: str
    video_id: str = Field(..., description="References videos.video_id")
    position: int = Field(..., ge=1, description="1-based queue ordering")
    scheduled_at: Optional[datetime] = Field(
        None,
        description="The exact datetime (timezone-aware) when this video should be published on YouTube",
    )
    added_at: datetime = Field(default_factory=datetime.utcnow)
