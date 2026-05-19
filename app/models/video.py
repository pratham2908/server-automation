"""Video and VideoQueue Pydantic models."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_serializer

from app.timezone import now_ist, to_ist_iso


class VideoStatus(str, Enum):
    """Allowed lifecycle states for a video."""

    TODO = "todo"
    READY = "ready"
    QUEUED = "queued"  # In internal queue — background worker will upload to platform
    SCHEDULED = "scheduled"  # Confirmed on platform (YouTube private+publishAt, or Instagram published)
    PUBLISHED = "published"


class AIPackagingStatus(str, Enum):
    """Status of the AI packaging (titles, thumbs, etc.) analysis."""

    PENDING = "pending"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"


class VideoMetadata(BaseModel):
    """Performance metrics (populated during sync / stats fetch).

    Shared fields (views, likes, comments, etc.) are populated for both
    YouTube and Instagram.  Platform-specific fields are null on the other
    platform.
    """

    # Shared
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    duration_seconds: int | None = None
    engagement_rate: float | None = None
    like_rate: float | None = None
    comment_rate: float | None = None
    # YouTube-specific
    youtube_privacy_status: str | None = Field(
        None,
        description="YouTube Data API status.privacyStatus: public, unlisted, or private",
    )
    avg_percentage_viewed: float | None = None
    avg_view_duration_seconds: int | None = None
    estimated_minutes_watched: float | None = None
    # Instagram-specific
    shares: int | None = None
    saves: int | None = None
    reach: int | None = None


class Video(BaseModel):
    """Full video document stored in the ``videos`` collection."""

    channel_id: str
    video_id: str = Field(..., description="Auto-generated UUID")
    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    category: str = ""
    status: VideoStatus = VideoStatus.TODO
    suggested: bool = False
    youtube_video_id: str | None = None
    instagram_media_id: str | None = None
    r2_object_key: str | None = None
    thumbnail_url: str | None = Field(None, description="Direct URL to the video thumbnail")
    packaging_status: AIPackagingStatus = AIPackagingStatus.PENDING
    ai_packaging: dict | None = Field(
        None,
        description="Gemini-generated content packaging (suggested_titles, suggested_description, suggested_tags, best_thumbnail_timestamp, thumbnail_url, reasoning)",
    )
    metadata: VideoMetadata = Field(default_factory=VideoMetadata)
    content_params: dict[str, str] | None = Field(
        None,
        description="Channel-specific content dimensions (e.g. simulation_type, challenge_mechanic, music)",
    )
    verification_status: str | None = Field(
        None,
        description="'unverified' when AI-assigned (category + content_params), 'verified' when user-confirmed or system-defined",
    )
    scheduled_at: datetime | None = Field(
        None,
        description="When the video is scheduled to go live. Set when scheduled.",
    )
    published_at: datetime | None = Field(
        None,
        description="When the video was published on the platform. Null until published.",
    )

    # Repost tracking
    is_repost: bool = False
    original_video_id: str | None = Field(
        None, description="video_id of the original video this was reposted from"
    )
    repost_count: int = 0  # Number of times this original video has been reposted
    repost_index: int | None = Field(
        None, description="Which repost this is (1st, 2nd, etc.)"
    )

    # Unified Analytics Data
    retention: dict[str, Any] | None = Field(
        None, description="Pre-publish multimodal analysis and predicted retention curve."
    )
    performance: dict[str, Any] | None = Field(
        None, description="Post-publish metric analysis and actual performance rating."
    )

    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)

    @field_serializer("scheduled_at", "published_at", "created_at", "updated_at")
    def _serialize_dt_ist(self, dt: datetime | None) -> str | None:
        """Serialize datetimes in GMT+5:30 (IST) for API responses."""
        return to_ist_iso(dt)


class VideoCreate(BaseModel):
    """Payload accepted when adding a new video to the queue."""

    title: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    category: str = ""


class VideoStatusUpdate(BaseModel):
    """Body for the PATCH status endpoint."""

    status: VideoStatus


class PostingQueue(BaseModel):
    """An entry in the ready queue (``posting_queue`` collection).

    Videos with status ``ready`` sit here after being uploaded to R2,
    waiting to be scheduled on the target platform.
    """

    channel_id: str
    video_id: str = Field(..., description="References videos.video_id")
    position: int = Field(..., ge=1, description="1-based queue ordering")
    added_at: datetime = Field(default_factory=now_ist)

    @field_serializer("added_at")
    def _serialize_dt_ist(self, dt: datetime) -> str:
        return to_ist_iso(dt) or ""


class ScheduleQueue(BaseModel):
    """An entry in the scheduled queue (``schedule_queue`` collection).

    Videos with status ``scheduled`` sit here after being uploaded to
    YouTube (private with ``publishAt``) or queued for Instagram auto-publishing.
    """

    channel_id: str
    video_id: str = Field(..., description="References videos.video_id")
    position: int = Field(..., ge=1, description="1-based queue ordering")
    scheduled_at: datetime | None = Field(
        None,
        description="The exact datetime (timezone-aware) when this video should be published",
    )
    added_at: datetime = Field(default_factory=now_ist)

    @field_serializer("scheduled_at", "added_at")
    def _serialize_dt_ist(self, dt: datetime | None) -> str | None:
        """Serialize datetimes in GMT+5:30 (IST) for API responses."""
        return to_ist_iso(dt)
