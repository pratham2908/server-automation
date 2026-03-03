"""Video and VideoQueue Pydantic models."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class VideoStatus(str, Enum):
    """Allowed lifecycle states for a video."""

    TODO = "todo"
    IN_QUEUE = "in_queue"
    DONE = "done"


class VideoMetadata(BaseModel):
    """YouTube performance metrics (populated after upload + stats fetch)."""

    views: Optional[int] = None
    engagement: Optional[float] = None
    avg_percentage_viewed: Optional[float] = None


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


class VideoQueue(BaseModel):
    """An entry in the ``video_queue`` collection (posting order)."""

    channel_id: str
    video_id: str = Field(..., description="References videos.video_id")
    position: int = Field(..., ge=1, description="1-based queue ordering")
    added_at: datetime = Field(default_factory=datetime.utcnow)
