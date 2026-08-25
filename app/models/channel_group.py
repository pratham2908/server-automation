"""Channel groups — the same brand across several platforms.

A brand usually lives on more than one platform, posting more or less the same
content to each. Every publishing flow already lets you tick several channels,
which means restating that relationship by hand every single time: the Geo
Ranking pair alone has been re-selected across fifty reposts.

A group states it once. Flows then default to the whole group, and the choice
stays visible and overridable rather than becoming a hidden rule.

One channel belongs to at most one group, so "who else gets this video" always
has a single answer.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.timezone import now_ist


class ChannelGroup(BaseModel):
    """Persisted in the ``channel_groups`` collection."""

    group_id: str = Field(..., description="Internal unique identifier")
    name: str = Field(..., min_length=1, max_length=80, description="Brand name, e.g. 'Geo Ranking'")
    primary_channel_id: str = Field(
        ...,
        description="The channel a video starts on by default; the others are the expansion",
    )
    channel_ids: list[str] = Field(..., min_length=1, description="Every channel in the group, primary included")
    auto_target: bool = Field(
        True,
        description="Whether flows pre-select the whole group. Off makes it a label without behaviour",
    )

    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)

    @model_validator(mode="after")
    def _primary_is_a_member(self) -> ChannelGroup:
        # A primary outside the group would silently drop out of its own expansion.
        if self.primary_channel_id not in self.channel_ids:
            raise ValueError("primary_channel_id must be one of channel_ids")
        if len(set(self.channel_ids)) != len(self.channel_ids):
            raise ValueError("channel_ids must not repeat")
        return self

    def others(self, channel_id: str) -> list[str]:
        """The rest of the group — who a video on *channel_id* should expand to."""
        return [cid for cid in self.channel_ids if cid != channel_id]


class GroupChannel(BaseModel):
    """A group member, resolved for display."""

    channel_id: str
    name: str
    platform: str
    handle: str | None = None
    thumbnail_url: str | None = None
    is_primary: bool = False


class ChannelGroupPublic(BaseModel):
    """A group with its members resolved, for the API."""

    group_id: str
    name: str
    primary_channel_id: str
    auto_target: bool
    channels: list[GroupChannel]
    created_at: datetime
    updated_at: datetime


class ChannelGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    primary_channel_id: str
    channel_ids: list[str] = Field(..., min_length=2, description="A group of one has nothing to expand to")
    auto_target: bool = True


class ChannelGroupUpdate(BaseModel):
    """Every field optional — only what is sent is changed."""

    name: str | None = Field(None, min_length=1, max_length=80)
    primary_channel_id: str | None = None
    channel_ids: list[str] | None = Field(None, min_length=2)
    auto_target: bool | None = None


class SuggestedGroup(BaseModel):
    """A group we think exists, offered for confirmation. Never created on its own."""

    name: str
    primary_channel_id: str
    channel_ids: list[str]
    channels: list[GroupChannel]
    reason: str = Field(..., description="Why these were matched, so the suggestion can be judged")
    confidence: float = Field(..., ge=0.0, le=1.0)
