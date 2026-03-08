"""Channel Pydantic model."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.timezone import now_ist


class ContentParam(BaseModel):
    """A single dimension in a channel's content schema."""

    name: str = Field(..., description="Parameter key, e.g. 'simulation_type'")
    description: str = Field("", description="What this parameter represents")
    values: list[str] = Field(
        default_factory=list,
        description="Allowed values. Empty list means free-form.",
    )


class Channel(BaseModel):
    """Represents a YouTube channel managed by the automation system."""

    channel_id: str = Field(..., description="Internal unique identifier")
    name: str = Field(..., description="Human-readable channel name")
    youtube_channel_id: str = Field(..., description="YouTube UC... channel ID")
    content_schema: list[ContentParam] = Field(
        default_factory=list,
        description="Custom content parameter definitions for this channel",
    )
    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)
