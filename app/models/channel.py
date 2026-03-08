"""Channel Pydantic model."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.timezone import now_ist


class Channel(BaseModel):
    """Represents a YouTube channel managed by the automation system."""

    channel_id: str = Field(..., description="Internal unique identifier")
    name: str = Field(..., description="Human-readable channel name")
    youtube_channel_id: str = Field(..., description="YouTube UC... channel ID")
    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)
