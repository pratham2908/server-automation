"""Channel and ContentParamDefinition Pydantic models."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.timezone import now_ist


class ContentParamValue(BaseModel):
    """A tracked value for a content param with its performance data."""

    value: str = Field(..., description="The actual value string, e.g. 'battle'")
    score: float = Field(0, description="Performance score (avg performance_rating of videos using this value)")
    video_count: int = Field(0, description="Number of published videos with this value")


class ContentParamDefinition(BaseModel):
    """A content param dimension stored in the ``content_params`` collection.

    Replaces the old channel-level ``content_schema`` entries.
    Free-form params have an empty ``values`` list.
    """

    channel_id: str
    name: str = Field(..., description="Parameter key, e.g. 'simulation_type'")
    description: str = Field("", description="What this parameter represents")
    values: list[ContentParamValue] = Field(
        default_factory=list,
        description="Tracked values with scores. Empty list = free-form param.",
    )
    belongs_to: list[str] = Field(
        default_factory=lambda: ["all"],
        description="Categories this param applies to. ['all'] = every category.",
    )
    unique: bool = Field(
        False,
        description="If True, Gemini must not reuse existing values for this param when generating new videos",
    )
    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)


class Competitor(BaseModel):
    """A competitor (YouTube or Instagram) tracked for a managed channel."""

    channel_id: str = Field(..., description="Parent channel this competitor belongs to")
    platform: str = Field("youtube", description="'youtube' or 'instagram'")

    # YouTube (optional)
    youtube_channel_id: Optional[str] = Field(None, description="Competitor's YouTube UC... ID")
    handle: Optional[str] = Field(None, description="YouTube handle, e.g. @MrBeast")

    # Instagram (optional)
    instagram_username: Optional[str] = Field(None, description="Instagram username, e.g. 'mrbeast'")
    instagram_user_id: Optional[str] = Field(None, description="Instagram ID (if known)")

    name: str = Field(..., description="Display name")
    thumbnail: str = Field("", description="Thumbnail/avatar URL")

    # Stats
    subscriber_count: int = Field(0, description="Follower count")
    video_count: int = Field(0, description="Media count")

    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)


class YouTubeTokens(BaseModel):
    """OAuth2 tokens for a channel's YouTube account, stored on the channel doc."""

    token: str = Field(..., description="OAuth2 access token")
    refresh_token: str = Field(..., description="OAuth2 refresh token")
    token_uri: str = Field("https://oauth2.googleapis.com/token")
    scopes: list[str] = Field(default_factory=list)
    expiry: Optional[str] = Field(None, description="ISO 8601 expiry datetime")


class InstagramTokens(BaseModel):
    """Facebook/Instagram long-lived token stored on the channel doc."""

    access_token: str = Field(..., description="Long-lived Facebook user access token")
    token_type: str = Field("bearer")
    expires_at: Optional[str] = Field(None, description="ISO 8601 expiry datetime")


class Channel(BaseModel):
    """Represents a channel (YouTube or Instagram) managed by the automation system."""

    channel_id: str = Field(..., description="Internal unique identifier")
    name: str = Field(..., description="Human-readable channel name")
    platform: str = Field("youtube", description="'youtube' or 'instagram'")
    youtube_channel_id: Optional[str] = Field(None, description="YouTube UC... channel ID (youtube only)")
    youtube_tokens: Optional[YouTubeTokens] = Field(None, description="YouTube OAuth tokens (excluded from API responses)")
    instagram_user_id: Optional[str] = Field(None, description="Instagram Graph API user ID (instagram only)")
    instagram_tokens: Optional[InstagramTokens] = Field(None, description="Instagram tokens (excluded from API responses)")
    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)
