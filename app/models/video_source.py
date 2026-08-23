"""VideoSource — the content app a channel pulls its finished videos from.

Each channel has a corresponding application that renders its content. Rather
than reaching into that app's storage, we call its API, which knows which renders
are actually finished and hands back a presigned MP4 URL per video.

Apps do not share one contract, so a source names its ``kind`` and carries a
config typed for that kind. Adding a third app means a new config model and a new
adapter — never a branch inside shared code, and never a change to a channel that
already works.

Secrets live on the config and are never returned by the API — see
:class:`VideoSourcePublic`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from app.timezone import now_ist

SourceKind = Literal["georank", "vidforge"]
AuthStyle = Literal["bearer", "api_key_header"]
SourceHealth = Literal["unknown", "ok", "error"]

# Renders in this state have a playable file; anything else is still cooking.
# Both apps happen to use the same word, but each config can override it.
COMPLETED_STATUS = "completed"


def _require_id_placeholder(value: str, field: str, *, allow_empty: bool = False) -> str:
    """A path we substitute an id into is useless without somewhere to put it."""
    if allow_empty and not value:
        return value
    if "{id}" not in value:
        suffix = ", or be empty to disable" if allow_empty else ""
        raise ValueError(f"{field} must contain the '{{id}}' placeholder{suffix}")
    return value


class GeoRankConfig(BaseModel):
    """An app exposing the read-only export feed contract.

    Cursor-paginated, authenticated with one static shared secret, and able to
    report and accept delivery state.
    """

    kind: Literal["georank"] = "georank"

    list_path: str = Field("/api/ext/videos", description="Page of finished videos")
    detail_path: str = Field("/api/ext/videos/{id}", description="One video with a fresh downloadUrl")
    mark_imported_path: str = Field(
        "/api/ext/videos/{id}/imported",
        description="POSTed after ingest to close the pull loop; empty disables it",
    )

    api_key: str = Field(..., description="Shared secret (never returned by the API)")
    auth_style: AuthStyle = Field("bearer", description="Send the secret as a Bearer token or as X-Api-Key")

    page_limit: int = Field(50, ge=1, le=100, description="Videos requested per page")

    @field_validator("detail_path")
    @classmethod
    def _detail(cls, v: str) -> str:
        return _require_id_placeholder(v, "detail_path")

    @field_validator("mark_imported_path")
    @classmethod
    def _mark(cls, v: str) -> str:
        return _require_id_placeholder(v, "mark_imported_path", allow_empty=True)


class VidForgeConfig(BaseModel):
    """A VidForge studio library.

    Page-numbered rather than cursor-based, and authenticated with an account
    login that returns a short-lived token, so there is no static secret to hold.

    ``video_kind``/``status`` are the server-side filters that separate finished
    renders from the whole job history; they are configurable because which
    filter means "publishable" is a per-library decision, not a law.
    """

    kind: Literal["vidforge"] = "vidforge"

    login_path: str = Field("/api/auth/login", description="Exchanges credentials for an access token")
    list_path: str = Field("/api/videos", description="Paginated library listing")
    detail_path: str = Field("/api/videos/{id}", description="One video, including downloadUrl")
    mark_imported_path: str = Field(
        "/api/videos/{id}",
        description="PATCHed with the sent flag after ingest; empty disables it",
    )

    email: str = Field(..., description="Account email (never returned by the API)")
    password: str = Field(..., description="Account password (never returned by the API)")
    app_key: str = Field("clips", description="Which app's profile to read; decides the library")

    video_kind: str | None = Field("edited", description="'edited' = finished renders. None for all kinds")
    status: str | None = Field(COMPLETED_STATUS, description="Only videos in this pipeline status")
    sent_flag_field: str = Field(
        "alreadySentToChannel",
        description="Field carrying delivery state, used both as a filter and in the mark payload",
    )
    page_limit: int = Field(50, ge=1, le=100, description="Videos requested per page")

    @field_validator("detail_path")
    @classmethod
    def _detail(cls, v: str) -> str:
        return _require_id_placeholder(v, "detail_path")

    @field_validator("mark_imported_path")
    @classmethod
    def _mark(cls, v: str) -> str:
        return _require_id_placeholder(v, "mark_imported_path", allow_empty=True)


SourceConfig = Annotated[GeoRankConfig | VidForgeConfig, Field(discriminator="kind")]


def _normalise_base_url(v: str) -> str:
    # Paths are joined verbatim, so a trailing slash here would produce '//api'.
    if not v.startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    return v.rstrip("/")


class VideoSource(BaseModel):
    """A channel's content app. Persisted in the ``video_sources`` collection."""

    source_id: str = Field(..., description="Internal unique identifier")
    channel_id: str = Field(..., description="Channel this app supplies videos for")
    name: str = Field(..., description="Human-readable label, e.g. 'GeoRank renderer'")

    base_url: str = Field(..., description="App origin, e.g. https://georank-server-xxx.run.app")
    config: SourceConfig = Field(..., description="Settings and credentials for this source's kind")

    enabled: bool = Field(True, description="Disabled sources are hidden from the import UI")

    # --- Connection health, refreshed whenever the source is tested or listed ---
    last_checked_at: datetime | None = Field(None, description="When the app was last reached")
    last_status: SourceHealth = Field("unknown", description="Result of the last connection check")
    last_error: str | None = Field(None, description="Error message from the last failed check")

    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)

    @property
    def kind(self) -> SourceKind:
        return self.config.kind

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return _normalise_base_url(v)


class VideoSourcePublic(BaseModel):
    """API-safe projection of a :class:`VideoSource` — no credentials.

    Credentials live on ``config``, which is deliberately not a field here, so no
    serialisation path can emit them even if a future config gains a secret.
    """

    source_id: str
    channel_id: str
    name: str
    kind: SourceKind
    base_url: str
    list_path: str
    credential_hint: str = Field(..., description="How this source authenticates, with the secret redacted")
    supports_mark_imported: bool = Field(..., description="Whether we can tell the app we ingested a video")
    enabled: bool
    last_checked_at: datetime | None
    last_status: SourceHealth
    last_error: str | None


class SourceCreateRequest(BaseModel):
    """Register a channel's content app from the UI.

    Carries credentials, so it is the one request in this module that must never
    be logged or echoed back — the response is a :class:`VideoSourcePublic`.
    """

    name: str = Field(..., min_length=1, max_length=80, description="Display label, e.g. 'VidForge clips'")
    base_url: str = Field(..., description="App origin, e.g. https://xyz.run.app")
    config: SourceConfig = Field(..., description="Settings and credentials for the chosen kind")

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return _normalise_base_url(v)


class SourceVideo(BaseModel):
    """One finished video, normalised from whatever shape its app returned."""

    id: str = Field(..., description="Stable id in the source app — the dedup key")
    title: str
    status: str = Field(..., description="Render status; only a completed one is importable")
    duration_ms: int | None = None
    created_at: str | None = Field(None, description="ISO timestamp from the app")
    thumbnail_url: str | None = None
    content_type: str = "video/mp4"
    size_bytes: int | None = None
    already_sent_to_channel: bool = Field(False, description="The app's own view of whether it gave us this")
    external_video_id: str | None = Field(None, description="The video_id the app thinks it created here")

    # Apps that organise their output — an episode, a series, a project — say so
    # here. What a group *means* is the app's business; the importer only needs to
    # know which videos belong together and what to call the bundle.
    group_id: str | None = Field(None, description="Videos sharing this belong together; None means ungrouped")
    group_label: str | None = Field(None, description="Human name for the group, when the app implies one")

    imported: bool = Field(False, description="True if this channel already has a video record for it")
    video_id: str | None = Field(None, description="Our video_id, when already present")
    delivered_by: Literal["import", "push", None] = Field(
        None,
        description="How it got here: pulled by this importer, or pushed by the app",
    )


class SourceListResponse(BaseModel):
    """A page of an app's finished videos."""

    videos: list[SourceVideo]
    next_cursor: str | None
    url_ttl_seconds: int | None = None
    group_noun: str | None = Field(
        None,
        description="What this app calls a group, e.g. 'Episode'. None when it does not group at all",
    )


class ImportRequest(BaseModel):
    """Request to pull one or more of the app's videos into our R2."""

    video_ids: list[str] = Field(..., min_length=1, description="Source video ids to import")
    scheduled_at: str | None = Field(None, description="Optional ISO datetime to schedule the videos to")
    analyze: bool = Field(True, description="Run AI packaging/retention analysis once the transfer lands")
