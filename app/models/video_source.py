"""VideoSource — the content app a channel pulls its finished videos from.

Each channel has a corresponding application that renders its content. Rather
than reaching into that app's storage, we call its export API, which knows which
renders are actually finished and hands back a presigned MP4 URL per video.

Every app implements the same response schema; only the host, the paths, and the
shared secret differ per channel, so all of that lives on this document.

The secret is stored here the same way ``youtube_tokens`` and ``instagram_tokens``
already live on the channel doc. It is never returned by the API — see
:class:`VideoSourcePublic`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.timezone import now_ist

AuthStyle = Literal["bearer", "api_key_header"]
SourceHealth = Literal["unknown", "ok", "error"]

# The app marks a render finished with this status; anything else is still cooking.
COMPLETED_STATUS = "completed"


class VideoSource(BaseModel):
    """A channel's content app. Persisted in the ``video_sources`` collection."""

    source_id: str = Field(..., description="Internal unique identifier")
    channel_id: str = Field(..., description="Channel this app supplies videos for")
    name: str = Field(..., description="Human-readable label, e.g. 'GeoRank renderer'")

    base_url: str = Field(..., description="App origin, e.g. https://georank-server-xxx.run.app")
    list_path: str = Field("/api/ext/videos", description="Path returning a page of finished videos")
    detail_path: str = Field(
        "/api/ext/videos/{id}",
        description="Path for one video with a freshly minted URL; '{id}' is substituted",
    )

    api_key: str = Field(..., description="Shared secret for this app (never returned by the API)")
    auth_style: AuthStyle = Field("bearer", description="Send the secret as a Bearer token or as X-Api-Key")

    enabled: bool = Field(True, description="Disabled sources are hidden from the import UI")

    # --- Connection health, refreshed whenever the source is tested or listed ---
    last_checked_at: datetime | None = Field(None, description="When the app was last reached")
    last_status: SourceHealth = Field("unknown", description="Result of the last connection check")
    last_error: str | None = Field(None, description="Error message from the last failed check")

    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        # Paths are joined verbatim, so a trailing slash here would produce '//api'.
        if not v.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v.rstrip("/")

    @field_validator("detail_path")
    @classmethod
    def _require_id_placeholder(cls, v: str) -> str:
        if "{id}" not in v:
            raise ValueError("detail_path must contain the '{id}' placeholder")
        return v


class VideoSourcePublic(BaseModel):
    """API-safe projection of a :class:`VideoSource` — no shared secret."""

    source_id: str
    channel_id: str
    name: str
    base_url: str
    list_path: str
    auth_style: AuthStyle
    api_key_masked: str = Field(..., description="Last four characters of the secret, for identification")
    enabled: bool
    last_checked_at: datetime | None
    last_status: SourceHealth
    last_error: str | None


class SourceVideo(BaseModel):
    """One finished video as reported by a channel's app.

    Field names mirror the app's JSON (camelCase) but are normalised to snake_case
    here so the rest of the server stays in one convention.
    """

    id: str = Field(..., description="Stable render id from the app — the dedup key")
    title: str
    status: str = Field(..., description="Render status; only 'completed' is importable")
    duration_ms: int | None = None
    created_at: str | None = Field(None, description="ISO timestamp from the app")
    thumbnail_url: str | None = None
    content_type: str = "video/mp4"
    already_sent_to_channel: bool = Field(False, description="The app's own view of whether it gave us this")
    external_video_id: str | None = Field(
        None,
        description="The video_id the app believes it already created in our system, if any",
    )

    imported: bool = Field(False, description="True if this render already has a video record on this channel")
    video_id: str | None = Field(None, description="Our video_id, when already present")
    delivered_by: Literal["import", "push", None] = Field(
        None,
        description="How it got here: pulled by this importer, or pushed by the app via the external API",
    )


class SourceListResponse(BaseModel):
    """A page of a channel app's finished videos."""

    videos: list[SourceVideo]
    next_cursor: str | None
    url_ttl_seconds: int | None = None


class ImportRequest(BaseModel):
    """Request to pull one or more of the app's videos into our R2."""

    video_ids: list[str] = Field(..., min_length=1, description="Source render ids to import")
    scheduled_at: str | None = Field(None, description="Optional ISO datetime to schedule the videos to")
    analyze: bool = Field(True, description="Run AI packaging/retention analysis once the transfer lands")
