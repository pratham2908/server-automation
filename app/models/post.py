"""Instagram image / carousel / story posts.

A post is many ordered assets under one caption, so it is its own document in
the ``posts`` collection rather than a variant of a ``videos`` record. The
contract with the analyzer UI is ``docs/instagram-posts.md``; the field names
and literal values here are that contract, so change both together.

``PostDoc`` / ``Slide`` are the stored shape (read back from Mongo), the
``*Out`` models are what the API returns, and the rest are request bodies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.timezone import assume_utc

PostKind = Literal["image", "carousel", "story"]
PostStatus = Literal["draft", "scheduled", "publishing", "published", "failed", "awaiting_manual", "archived"]
MusicMode = Literal["none", "in_app"]
SlideMediaType = Literal["image", "video"]
FirstCommentStatus = Literal["posted", "failed"]

POST_STATUSES: tuple[str, ...] = (
    "draft",
    "scheduled",
    "publishing",
    "published",
    "failed",
    "awaiting_manual",
    "archived",
)

# Mongo hands datetimes back naive (UTC). Making them aware on the way in means
# nothing downstream can compare a naive value against an aware ``now``.
AwareDatetime = Annotated[datetime, AfterValidator(assume_utc)]


# ------------------------------------------------------------------
# Stored shape
# ------------------------------------------------------------------


class Slide(BaseModel):
    model_config = ConfigDict(extra="ignore")

    slide_id: str
    media_type: SlideMediaType
    content_type: str
    r2_object_key: str
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    size_bytes: int | None = None
    alt_text: str | None = None
    uploaded: bool = False


class ChildContainer(BaseModel):
    """One carousel item container, tied to the slide it was made from."""

    model_config = ConfigDict(extra="ignore")

    slide_id: str
    container_id: str
    # Beyond the contract's minimum: when it was made (containers expire after
    # 24 h) and whether Instagram already reported it FINISHED, so a resumed
    # publish neither re-polls finished items nor trusts an expired one.
    created_at: AwareDatetime | None = None
    finished: bool = False


class PublishState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    started_at: AwareDatetime
    children: list[ChildContainer] = Field(default_factory=list)
    container_id: str | None = None
    container_created_at: AwareDatetime | None = None


class PostDoc(BaseModel):
    model_config = ConfigDict(extra="ignore")

    post_id: str
    channel_id: str
    kind: PostKind
    caption: str = ""
    first_comment: str | None = None
    first_comment_status: FirstCommentStatus | None = None
    music_mode: MusicMode = "none"
    music_note: str | None = None
    slides: list[Slide] = Field(default_factory=list)
    status: PostStatus = "draft"
    scheduled_at: AwareDatetime | None = None
    published_at: AwareDatetime | None = None
    instagram_media_id: str | None = None
    permalink: str | None = None
    publish_state: PublishState | None = None
    attempts: int = 0
    last_error: str | None = None
    handoff_sent_at: AwareDatetime | None = None
    archived_from_status: PostStatus | None = None
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None


# ------------------------------------------------------------------
# Request bodies
# ------------------------------------------------------------------


class PostCreate(BaseModel):
    kind: PostKind
    caption: str = ""
    first_comment: str | None = None
    music_mode: MusicMode = "none"
    music_note: str | None = None


class PostUpdate(BaseModel):
    kind: PostKind | None = None
    caption: str | None = None
    first_comment: str | None = None
    music_mode: MusicMode | None = None
    music_note: str | None = None
    slide_order: list[str] | None = None
    alt_texts: dict[str, str] | None = None


class SlideCreate(BaseModel):
    media_type: SlideMediaType
    # A plain string rather than a Literal so a disallowed type gets a readable
    # 400 from the upload rules instead of a pydantic 422.
    content_type: str
    size_bytes: int = Field(..., ge=0)
    width: int | None = Field(None, ge=1)
    height: int | None = Field(None, ge=1)
    duration_seconds: float | None = Field(None, ge=0)


class ScheduleRequest(BaseModel):
    scheduled_at: str


class MarkPublishedRequest(BaseModel):
    permalink: str | None = None


# ------------------------------------------------------------------
# Responses
# ------------------------------------------------------------------


class SlideOut(BaseModel):
    slide_id: str
    media_type: SlideMediaType
    content_type: str
    width: int | None
    height: int | None
    duration_seconds: float | None
    size_bytes: int | None
    alt_text: str | None
    uploaded: bool
    preview_url: str | None


class PostOut(BaseModel):
    post_id: str
    channel_id: str
    kind: PostKind
    status: PostStatus
    caption: str
    first_comment: str | None
    first_comment_status: FirstCommentStatus | None
    music_mode: MusicMode
    music_note: str | None
    slides: list[SlideOut]
    scheduled_at: str | None
    published_at: str | None
    instagram_media_id: str | None
    permalink: str | None
    last_error: str | None
    attempts: int
    handoff_sent_at: str | None
    problems: list[str]
    warnings: list[str]
    created_at: str | None
    updated_at: str | None


class PostListOut(BaseModel):
    posts: list[PostOut]


class SlideUploadOut(BaseModel):
    slide: SlideOut
    upload_url: str
    upload_headers: dict[str, str]


class DeletedOut(BaseModel):
    deleted: bool


class PublishingLimitOut(BaseModel):
    quota_total: int
    quota_usage: int
    quota_duration_seconds: int


class FeedChild(BaseModel):
    media_type: str
    url: str | None


class FeedItem(BaseModel):
    instagram_media_id: str
    media_type: str
    media_product_type: str | None
    caption: str
    permalink: str | None
    timestamp: str | None
    thumbnail_url: str | None
    like_count: int
    comments_count: int
    children: list[FeedChild]
    post_id: str | None


class FeedPage(BaseModel):
    items: list[FeedItem]
    next_cursor: str | None


class MediaInsightsOut(BaseModel):
    media_id: str
    metrics: dict[str, int]
    unavailable: list[str]


class HandoffSlide(BaseModel):
    slide_id: str
    media_type: SlideMediaType
    content_type: str
    filename: str
    download_url: str


class HandoffOut(BaseModel):
    post_id: str
    channel_id: str
    # Which Instagram account to post from — the share sheet can't pick it.
    channel_name: str
    instagram_username: str | None
    kind: PostKind
    caption: str
    first_comment: str | None
    music_note: str | None
    status: PostStatus
    scheduled_at: str | None
    permalink: str | None
    slides: list[HandoffSlide]
