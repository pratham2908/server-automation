"""VideoSource — an external S3-compatible bucket a channel pulls videos from.

Lets a channel register where its finished videos already live (Cloudflare R2 or
AWS S3) so the server can transfer them straight into our own R2, instead of a
human downloading a file and re-uploading it through the browser.

Credentials are stored on the document, matching how ``youtube_tokens`` and
``instagram_tokens`` are already persisted on the channel doc. The secret is
never returned by the API — see ``VideoSourcePublic``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.timezone import now_ist

SourceProvider = Literal["r2", "s3"]
SourceHealth = Literal["unknown", "ok", "error"]


class VideoSource(BaseModel):
    """A registered external bucket. Persisted in the ``video_sources`` collection."""

    source_id: str = Field(..., description="Internal unique identifier")
    channel_id: str = Field(..., description="Channel this source supplies videos for")
    name: str = Field(..., description="Human-readable label, e.g. 'Editor drop bucket'")

    provider: SourceProvider = Field(..., description="'r2' or 's3'")
    bucket: str = Field(..., description="Bucket name")
    access_key_id: str = Field(..., description="Access key ID")
    secret_access_key: str = Field(..., description="Secret access key (never returned by the API)")

    endpoint_url: str | None = Field(
        None,
        description="Required for R2 (https://<account>.r2.cloudflarestorage.com). None for real AWS S3.",
    )
    region: str = Field("auto", description="'auto' for R2, a real region (e.g. 'ap-south-1') for S3")
    prefix: str = Field("", description="Restrict browsing to keys under this prefix")

    enabled: bool = Field(True, description="Disabled sources are hidden from the import UI")

    # --- Connection health, refreshed whenever the source is tested or browsed ---
    last_checked_at: datetime | None = Field(None, description="When the connection was last verified")
    last_status: SourceHealth = Field("unknown", description="Result of the last connection check")
    last_error: str | None = Field(None, description="Error message from the last failed check")

    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)

    @model_validator(mode="after")
    def _check_endpoint(self) -> VideoSource:
        # boto3 can derive an endpoint for AWS S3 from the region, but never for
        # R2 — a missing endpoint there silently points the client at AWS.
        if self.provider == "r2" and not self.endpoint_url:
            raise ValueError("endpoint_url is required when provider is 'r2'")
        return self


class VideoSourcePublic(BaseModel):
    """API-safe projection of a :class:`VideoSource` — no secret key."""

    source_id: str
    channel_id: str
    name: str
    provider: SourceProvider
    bucket: str
    access_key_id_masked: str = Field(..., description="Access key ID with the middle redacted")
    endpoint_url: str | None
    region: str
    prefix: str
    enabled: bool
    last_checked_at: datetime | None
    last_status: SourceHealth
    last_error: str | None


class SourceObject(BaseModel):
    """One browsable object in a source bucket."""

    key: str
    size: int
    last_modified: datetime | None
    imported: bool = Field(False, description="True if this key already has a video record for the channel")
    video_id: str | None = Field(None, description="Our video_id, when already imported")


class SourceBrowseResponse(BaseModel):
    """A page of a source-bucket listing."""

    prefix: str
    folders: list[str]
    files: list[SourceObject]
    next_cursor: str | None
    is_truncated: bool


class ImportRequest(BaseModel):
    """Request to pull one or more source objects into our R2."""

    keys: list[str] = Field(..., min_length=1, description="Source object keys to import")
    scheduled_at: str | None = Field(None, description="Optional ISO datetime to schedule the videos to")
    analyze: bool = Field(True, description="Run AI packaging/retention analysis once the transfer lands")
