"""Pure rules for Instagram posts: validation, state transitions, naming, matching.

No I/O here — the service, the router and the publisher all ask this module
the same questions, and tests can answer them without a database. The limits
are Instagram's own (see ``docs/instagram-posts.md``); refusing at the edge is
cheaper than a container ERROR discovered at publish time.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from dateutil.parser import isoparse

from app.models.post import PostDoc, Slide, SlideCreate
from app.services.first_comment import validate_comment
from app.timezone import assume_utc

IMAGE_CONTENT_TYPE = "image/jpeg"
VIDEO_CONTENT_TYPE = "video/mp4"
CONTENT_TYPE_FOR_MEDIA = {"image": IMAGE_CONTENT_TYPE, "video": VIDEO_CONTENT_TYPE}
EXTENSION_FOR_CONTENT_TYPE = {IMAGE_CONTENT_TYPE: "jpg", VIDEO_CONTENT_TYPE: "mp4"}

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 300 * 1024 * 1024
MIN_IMAGE_WIDTH = 320
MAX_IMAGE_WIDTH = 1440
SOFT_IMAGE_WIDTH = 1080
MIN_ASPECT = 0.8  # 4:5
MAX_ASPECT = 1.91
MIN_VIDEO_SECONDS = 3.0
MAX_VIDEO_SECONDS = 60.0
MAX_CAPTION_CHARS = 2200
MAX_HASHTAGS = 30
MIN_CAROUSEL_SLIDES = 2
MAX_CAROUSEL_SLIDES = 10
STORY_ASPECT = 9 / 16
# How far an aspect may drift before we call it "different". Browser-side JPEG
# resizing rounds dimensions, so exact equality would warn on every carousel.
ASPECT_TOLERANCE = 0.02

# Handoff lead time for music_mode="in_app": long enough to open the phone and
# add a song, short enough that the email is still relevant.
HANDOFF_LEAD = timedelta(minutes=30)
# A publish that has not finished two hours after it started is not going to.
STUCK_AFTER = timedelta(hours=2)
# Containers expire at 24 h; recreate a little before that rather than race it.
CONTAINER_MAX_AGE = timedelta(hours=23)
# Manual posts are matched only if Instagram timestamps them after the handoff
# (minus slack for the owner posting early, or clocks disagreeing).
MANUAL_MATCH_SLACK = timedelta(hours=2)
CAPTION_MATCH_CHARS = 100

TERMINAL_CONTAINER_CODES = frozenset({"ERROR", "EXPIRED"})

_HASHTAG = re.compile(r"(?<![\w#])#\w+")

# ------------------------------------------------------------------
# State machine
# ------------------------------------------------------------------

TEXT_EDITABLE = frozenset({"draft", "scheduled", "failed", "awaiting_manual"})
SLIDES_EDITABLE = frozenset({"draft", "scheduled", "failed"})
DELETABLE = frozenset({"draft", "failed", "archived"})
SCHEDULABLE = frozenset({"draft", "scheduled"})
UNSCHEDULABLE = frozenset({"scheduled", "awaiting_manual"})
PUBLISH_NOW_FROM = frozenset({"draft", "scheduled"})


def can_edit_text(status: str) -> bool:
    """Caption, first comment and music fields."""
    return status in TEXT_EDITABLE


def can_edit_slides(status: str) -> bool:
    """Slides, their order and alt text, and the post kind."""
    return status in SLIDES_EDITABLE


def can_delete(status: str) -> bool:
    return status in DELETABLE


def can_schedule(status: str) -> bool:
    return status in SCHEDULABLE


def can_unschedule(status: str) -> bool:
    return status in UNSCHEDULABLE


def can_publish_now(status: str) -> bool:
    return status in PUBLISH_NOW_FROM


def can_retry(status: str) -> bool:
    return status == "failed"


def can_mark_published(status: str) -> bool:
    return status == "awaiting_manual"


def can_archive(status: str) -> bool:
    # A publish in flight holds live containers; archiving under it would leave
    # the worker publishing a post the UI says is put away.
    return status not in ("publishing", "archived")


def restore_status(archived_from: str | None, scheduled_at: datetime | None, now: datetime) -> str:
    """Where an archived post goes back to.

    A scheduled post whose time passed while archived becomes a draft: putting
    it straight back into ``scheduled`` would publish it the moment it is
    restored, which is not what "restore" promises.
    """
    target = archived_from or "draft"
    if target in ("archived", "publishing"):
        return "draft"
    if target == "scheduled" and (scheduled_at is None or assume_utc(scheduled_at) <= now):
        return "draft"
    return target


# ------------------------------------------------------------------
# Naming
# ------------------------------------------------------------------


def slide_key(channel_id: str, post_id: str, slide_id: str, content_type: str) -> str:
    """R2 key for a slide. Everything for a post sits under one prefix so it can be listed and purged together."""
    ext = EXTENSION_FOR_CONTENT_TYPE.get(content_type, "bin")
    return f"{channel_id}/posts/{post_id}/{slide_id}.{ext}"


def slide_filename(post_id: str, index: int, slide: Slide) -> str:
    """Download name on the phone: short, ordered, and with the right extension for the share sheet."""
    ext = EXTENSION_FOR_CONTENT_TYPE.get(slide.content_type, "bin")
    return f"post-{post_id[:8]}-{index + 1:02d}.{ext}"


# ------------------------------------------------------------------
# Text helpers
# ------------------------------------------------------------------


def count_hashtags(caption: str) -> int:
    return len(_HASHTAG.findall(caption or ""))


def normalise_caption(caption: str | None) -> str:
    """Caption reduced to what survives Instagram's app: case, spacing, and truncation-insensitive."""
    return " ".join((caption or "").lower().split())[:CAPTION_MATCH_CHARS]


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------


@dataclass
class Validation:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _aspect(slide: Slide) -> float | None:
    if slide.width and slide.height:
        return slide.width / slide.height
    return None


def _differs(a: float, b: float) -> bool:
    return abs(a - b) / b > ASPECT_TOLERANCE


def _slide_problems(post: PostDoc, n: int, slide: Slide) -> list[str]:
    label = f"Slide {n}"
    out: list[str] = []
    if not slide.uploaded:
        out.append(f"{label} hasn't finished uploading")
    if slide.media_type == "image":
        if slide.content_type != IMAGE_CONTENT_TYPE:
            out.append(f"{label} must be a JPEG image")
        if slide.size_bytes is not None and slide.size_bytes > MAX_IMAGE_BYTES:
            out.append(f"{label} is larger than 8 MB")
        if slide.width is not None and not MIN_IMAGE_WIDTH <= slide.width <= MAX_IMAGE_WIDTH:
            out.append(f"{label} must be {MIN_IMAGE_WIDTH}–{MAX_IMAGE_WIDTH} px wide (it is {slide.width} px)")
        aspect = _aspect(slide)
        if post.kind != "story" and aspect is not None and not MIN_ASPECT <= aspect <= MAX_ASPECT:
            out.append(f"{label} has an aspect ratio outside 4:5 … 1.91:1")
    else:
        if slide.content_type != VIDEO_CONTENT_TYPE:
            out.append(f"{label} must be an MP4 video")
        if slide.size_bytes is not None and slide.size_bytes > MAX_VIDEO_BYTES:
            out.append(f"{label} is larger than 300 MB")
        if (
            post.kind in ("carousel", "story")
            and slide.duration_seconds is not None
            and not MIN_VIDEO_SECONDS <= slide.duration_seconds <= MAX_VIDEO_SECONDS
        ):
            out.append(f"{label} must be 3–60 seconds long")
    return out


def validate(post: PostDoc) -> Validation:
    """Problems block scheduling; warnings are shown but never block."""
    result = Validation()
    slides = post.slides
    count = len(slides)

    if post.kind == "image":
        if count != 1 or slides[0].media_type != "image":
            result.problems.append("An image post needs exactly one image")
    elif post.kind == "story":
        if count != 1:
            result.problems.append("A story needs exactly one image or video")
    elif not MIN_CAROUSEL_SLIDES <= count <= MAX_CAROUSEL_SLIDES:
        result.problems.append(f"A carousel needs {MIN_CAROUSEL_SLIDES}–{MAX_CAROUSEL_SLIDES} slides (it has {count})")

    for n, slide in enumerate(slides, start=1):
        result.problems.extend(_slide_problems(post, n, slide))

    if post.kind != "story":
        if len(post.caption) > MAX_CAPTION_CHARS:
            result.problems.append(f"Caption is longer than {MAX_CAPTION_CHARS} characters")
        tags = count_hashtags(post.caption)
        if tags > MAX_HASHTAGS:
            result.problems.append(f"Caption has {tags} hashtags; Instagram allows {MAX_HASHTAGS}")

    try:
        validate_comment(post.first_comment, "instagram")
    except ValueError as exc:
        result.problems.append(f"First comment: {exc}")

    # ---- warnings ----
    if post.kind == "carousel" and slides:
        first = _aspect(slides[0])
        for n, slide in enumerate(slides[1:], start=2):
            aspect = _aspect(slide)
            if first is not None and aspect is not None and _differs(aspect, first):
                result.warnings.append(f"Slide {n} will be cropped to match slide 1")

    if post.kind == "story":
        for slide in slides:
            aspect = _aspect(slide)
            if aspect is not None and _differs(aspect, STORY_ASPECT):
                result.warnings.append("Stories are 9:16; this will be letterboxed")
        if post.caption.strip():
            result.warnings.append("Stories have no caption; it won't be posted")

    for n, slide in enumerate(slides, start=1):
        if slide.media_type == "image" and slide.width is not None and slide.width < SOFT_IMAGE_WIDTH:
            result.warnings.append(f"Slide {n} is narrower than {SOFT_IMAGE_WIDTH} px and may look soft on Instagram")

    return result


def max_slides(kind: str) -> int:
    return MAX_CAROUSEL_SLIDES if kind == "carousel" else 1


def upload_refusal(kind: str, existing_slides: int, req: SlideCreate) -> str | None:
    """Why we will not mint an upload URL for this slide, or ``None`` to go ahead.

    Checked before the URL exists so a wrong type or an oversized file never
    lands in R2 in the first place.
    """
    expected = CONTENT_TYPE_FOR_MEDIA[req.media_type]
    if req.content_type != expected:
        return f"A {req.media_type} slide must be {expected} (got {req.content_type or 'nothing'})"
    limit = MAX_IMAGE_BYTES if req.media_type == "image" else MAX_VIDEO_BYTES
    if req.size_bytes > limit:
        return f"File is {req.size_bytes} bytes; the limit for a {req.media_type} is {limit} bytes"
    if req.size_bytes == 0:
        return "File is empty"
    if kind == "image" and req.media_type != "image":
        return "An image post takes an image, not a video"
    if existing_slides >= max_slides(kind):
        return f"A {kind} post can have at most {max_slides(kind)} slide(s)"
    return None


def alt_text_refusal(kind: str, slide: Slide) -> str | None:
    if kind == "story":
        return "Stories do not take alt text"
    if slide.media_type != "image":
        return "Alt text applies to image slides only"
    return None


# ------------------------------------------------------------------
# Worker timing
# ------------------------------------------------------------------


def handoff_due(scheduled_at: datetime | None, now: datetime) -> bool:
    return scheduled_at is not None and assume_utc(scheduled_at) - HANDOFF_LEAD <= now


def is_stuck(started_at: datetime, now: datetime) -> bool:
    return now - assume_utc(started_at) >= STUCK_AFTER


def container_expired(created_at: datetime | None, now: datetime) -> bool:
    """Unknown age counts as fresh: we only discard what we know is about to lapse."""
    return created_at is not None and now - assume_utc(created_at) >= CONTAINER_MAX_AGE


# ------------------------------------------------------------------
# Manual-post detection
# ------------------------------------------------------------------


def media_time(item: Mapping[str, Any]) -> datetime | None:
    raw = item.get("timestamp")
    if not raw:
        return None
    try:
        return assume_utc(isoparse(str(raw)))
    except ValueError:
        return None


def match_manual_media(
    post: PostDoc,
    media: Iterable[Mapping[str, Any]],
    linked_media_ids: set[str],
) -> Mapping[str, Any] | None:
    """The Instagram media that is this hand-off post, if the owner has posted it.

    A post with no caption cannot be told apart from anything else the account
    publishes, so it is never auto-matched — "I've posted it" covers that case.
    """
    wanted = normalise_caption(post.caption)
    if not wanted or post.handoff_sent_at is None:
        return None
    earliest = post.handoff_sent_at - MANUAL_MATCH_SLACK
    for item in media:
        media_id = str(item.get("id") or "")
        if not media_id or media_id in linked_media_ids:
            continue
        posted_at = media_time(item)
        if posted_at is None or posted_at < earliest:
            continue
        if normalise_caption(item.get("caption")) == wanted:
            return item
    return None


# ------------------------------------------------------------------
# Storage purge
# ------------------------------------------------------------------


def protected_slide_keys(posts: Iterable[PostDoc]) -> set[str]:
    """R2 keys the storage purge must keep.

    Everything not yet on Instagram needs its files. An archived post counts by
    the status it was archived from: it can be restored, and a restored draft
    whose slides were purged would claim ``uploaded`` over missing objects.
    """
    keys: set[str] = set()
    for post in posts:
        effective = post.archived_from_status if post.status == "archived" else post.status
        if effective == "published":
            continue
        keys.update(slide.r2_object_key for slide in post.slides)
    return keys
