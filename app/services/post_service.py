"""Instagram posts: the database and R2 side of every route.

The rules (what is valid, which state may go where) live in ``post_rules``;
this module applies them to stored documents and does the I/O. Routes map the
``PostError`` family onto HTTP status codes and nothing else.

Every write that depends on the post's status is conditional on that status
(``{"post_id": ..., "status": <what we read>}``). The publisher claims posts the
same way, so an edit racing a claim loses cleanly with a 409 instead of
editing a post that is already being published.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from dateutil.parser import isoparse

from app.database import get_channel_platform
from app.logger import get_logger
from app.models.post import (
    POST_STATUSES,
    DeletedOut,
    FeedChild,
    FeedItem,
    FeedPage,
    HandoffOut,
    HandoffSlide,
    MarkPublishedRequest,
    MediaInsightsOut,
    PostCreate,
    PostDoc,
    PostListOut,
    PostOut,
    PostUpdate,
    PublishingLimitOut,
    Slide,
    SlideCreate,
    SlideOut,
    SlideUploadOut,
)
from app.services import post_rules as rules
from app.timezone import IST, now_ist, to_ist_iso

logger = get_logger(__name__)

PREVIEW_URL_TTL = 3600
HANDOFF_URL_TTL = 24 * 3600
INSIGHT_METRICS = ["views", "reach", "likes", "comments", "saved", "shares", "total_interactions"]
MAX_FEED_LIMIT = 50


class PostError(Exception):
    """A request the post rules refuse. ``status_code`` is the HTTP status to answer with."""

    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class PostNotFoundError(PostError):
    status_code = 404


class PostConflictError(PostError):
    status_code = 409


class UpstreamError(PostError):
    """Instagram or R2 failed while serving the request."""

    status_code = 502


def _noop() -> None:
    return None


def parse_scheduled_at(raw: str) -> datetime:
    """An ISO string from the UI; a naive one is IST, as for videos."""
    try:
        parsed = isoparse(raw)
    except ValueError:
        raise PostError(f"scheduled_at is not an ISO datetime: {raw!r}")
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=IST)


def _clean_comment(text: str | None) -> str | None:
    """Same normalisation as reels; length is left to validation so it shows as a problem, not a lost edit."""
    if text is None:
        return None
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return cleaned or None


def _clean_note(text: str | None) -> str | None:
    if text is None:
        return None
    return text.strip() or None


class PostService:
    def __init__(
        self,
        db: Any,
        r2: Any,
        instagram_manager: Any = None,
        wake: Callable[[], None] = _noop,
    ) -> None:
        self.db = db
        self.r2 = r2
        self.instagram_manager = instagram_manager
        self._wake = wake

    # ------------------------------------------------------------------
    # Loading and shaping
    # ------------------------------------------------------------------

    async def _channel(self, channel_id: str) -> dict[str, Any]:
        channel = await self.db.channels.find_one({"channel_id": channel_id})
        if not channel:
            raise PostError(f"Channel '{channel_id}' not found")
        if get_channel_platform(channel) != "instagram":
            raise PostError("Posts are only available on Instagram channels")
        return dict(channel)

    async def _load(self, channel_id: str, post_id: str) -> PostDoc:
        await self._channel(channel_id)
        doc = await self.db.posts.find_one({"channel_id": channel_id, "post_id": post_id})
        if not doc:
            raise PostNotFoundError(f"Post '{post_id}' not found")
        return PostDoc.model_validate(doc)

    def _slide_out(self, slide: Slide) -> SlideOut:
        preview = None
        if slide.uploaded and self.r2 is not None:
            # Signing is local (no network), so this is safe on the event loop.
            preview = self.r2.generate_presigned_url(slide.r2_object_key, expires_in=PREVIEW_URL_TTL)
        return SlideOut(
            slide_id=slide.slide_id,
            media_type=slide.media_type,
            content_type=slide.content_type,
            width=slide.width,
            height=slide.height,
            duration_seconds=slide.duration_seconds,
            size_bytes=slide.size_bytes,
            alt_text=slide.alt_text,
            uploaded=slide.uploaded,
            preview_url=preview,
        )

    def to_out(self, post: PostDoc) -> PostOut:
        checked = rules.validate(post)
        return PostOut(
            post_id=post.post_id,
            channel_id=post.channel_id,
            kind=post.kind,
            status=post.status,
            caption=post.caption,
            first_comment=post.first_comment,
            first_comment_status=post.first_comment_status,
            music_mode=post.music_mode,
            music_note=post.music_note,
            slides=[self._slide_out(s) for s in post.slides],
            scheduled_at=to_ist_iso(post.scheduled_at),
            published_at=to_ist_iso(post.published_at),
            instagram_media_id=post.instagram_media_id,
            permalink=post.permalink,
            last_error=post.last_error,
            attempts=post.attempts,
            handoff_sent_at=to_ist_iso(post.handoff_sent_at),
            problems=checked.problems,
            warnings=checked.warnings,
            created_at=to_ist_iso(post.created_at),
            updated_at=to_ist_iso(post.updated_at),
        )

    async def _write(self, post: PostDoc, fields: dict[str, Any]) -> PostOut:
        """Apply *fields* only if the post is still in the status we read it in."""
        fields = {**fields, "updated_at": now_ist()}
        result = await self.db.posts.update_one(
            {"channel_id": post.channel_id, "post_id": post.post_id, "status": post.status},
            {"$set": fields},
        )
        if result.matched_count == 0:
            raise PostConflictError("The post changed while this request ran — reload and try again")
        # Re-validated rather than model_copy'd: *fields* carries slides as the
        # plain dicts Mongo stores, and the response needs them as models.
        return self.to_out(PostDoc.model_validate({**post.model_dump(), **fields}))

    @staticmethod
    def _require(allowed: bool, post: PostDoc, action: str) -> None:
        if not allowed:
            raise PostConflictError(f"Cannot {action} a post that is {post.status}")

    @staticmethod
    def _refuse_if_problems(post: PostDoc, action: str) -> None:
        problems = rules.validate(post).problems
        if problems:
            raise PostError(f"Cannot {action}: " + "; ".join(problems))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def list_posts(self, channel_id: str, status: str | None) -> PostListOut:
        await self._channel(channel_id)
        query: dict[str, Any] = {"channel_id": channel_id}
        if status:
            if status not in POST_STATUSES:
                raise PostError(f"Unknown status '{status}'")
            query["status"] = status
        else:
            query["status"] = {"$ne": "archived"}
        docs = await self.db.posts.find(query).sort("updated_at", -1).to_list(length=None)
        return PostListOut(posts=[self.to_out(PostDoc.model_validate(d)) for d in docs])

    async def create_post(self, channel_id: str, body: PostCreate) -> PostOut:
        await self._channel(channel_id)
        now = now_ist()
        post = PostDoc(
            post_id=uuid.uuid4().hex,
            channel_id=channel_id,
            kind=body.kind,
            caption=body.caption,
            first_comment=_clean_comment(body.first_comment),
            music_mode=body.music_mode,
            music_note=_clean_note(body.music_note),
            status="draft",
            created_at=now,
            updated_at=now,
        )
        await self.db.posts.insert_one(post.model_dump())
        return self.to_out(post)

    async def get_post(self, channel_id: str, post_id: str) -> PostOut:
        return self.to_out(await self._load(channel_id, post_id))

    async def update_post(self, channel_id: str, post_id: str, body: PostUpdate) -> PostOut:
        post = await self._load(channel_id, post_id)
        sent = body.model_fields_set
        text_fields = {"caption", "first_comment", "music_mode", "music_note"} & sent
        slide_fields = {"kind", "slide_order", "alt_texts"} & sent
        if text_fields:
            self._require(rules.can_edit_text(post.status), post, "edit the caption or music of")
        if slide_fields:
            self._require(rules.can_edit_slides(post.status), post, "change the slides of")

        fields: dict[str, Any] = {}
        if "caption" in sent:
            fields["caption"] = body.caption or ""
        if "first_comment" in sent:
            fields["first_comment"] = _clean_comment(body.first_comment)
        if "music_mode" in sent and body.music_mode is not None:
            fields["music_mode"] = body.music_mode
        if "music_note" in sent:
            fields["music_note"] = _clean_note(body.music_note)
        if "kind" in sent and body.kind is not None:
            fields["kind"] = body.kind

        slides = list(post.slides)
        if "slide_order" in sent and body.slide_order is not None:
            by_id = {s.slide_id: s for s in slides}
            if sorted(body.slide_order) != sorted(by_id):
                raise PostError("slide_order must list every slide of the post exactly once")
            slides = [by_id[sid] for sid in body.slide_order]
        if "alt_texts" in sent and body.alt_texts:
            kind = fields.get("kind", post.kind)
            by_id = {s.slide_id: s for s in slides}
            for slide_id, text in body.alt_texts.items():
                slide = by_id.get(slide_id)
                if slide is None:
                    raise PostError(f"Slide '{slide_id}' is not part of this post")
                refusal = rules.alt_text_refusal(kind, slide)
                if refusal:
                    raise PostError(refusal)
                by_id[slide_id] = slide.model_copy(update={"alt_text": text.strip() or None})
            slides = [by_id[s.slide_id] for s in slides]
        if slides != list(post.slides):
            fields["slides"] = [s.model_dump() for s in slides]

        if not fields:
            return self.to_out(post)

        updated = post.model_copy(update={**fields, "slides": slides})
        if post.status == "scheduled":
            # A scheduled post has already passed validation; an edit must not
            # quietly turn it into one that fails at publish time.
            self._refuse_if_problems(updated, "save this edit to a scheduled post")
        return await self._write(post, fields)

    async def delete_post(self, channel_id: str, post_id: str) -> DeletedOut:
        post = await self._load(channel_id, post_id)
        self._require(rules.can_delete(post.status), post, "delete")
        for slide in post.slides:
            await self._delete_object(slide.r2_object_key)
        await self.db.posts.delete_one({"channel_id": channel_id, "post_id": post_id, "status": post.status})
        return DeletedOut(deleted=True)

    async def _delete_object(self, key: str) -> None:
        if self.r2 is None:
            return
        try:
            await asyncio.to_thread(self.r2.delete_video, key)
        except Exception as exc:
            # Not fatal: once the post is gone nothing protects the key, so the
            # storage purge collects the orphan later. Refusing the delete over a
            # storage hiccup would leave the user unable to remove the post.
            logger.warning("Could not delete R2 object '%s': %s — the storage purge will collect it", key, exc)

    # ------------------------------------------------------------------
    # Slides
    # ------------------------------------------------------------------

    async def create_slide(self, channel_id: str, post_id: str, body: SlideCreate) -> SlideUploadOut:
        post = await self._load(channel_id, post_id)
        self._require(rules.can_edit_slides(post.status), post, "add slides to")
        refusal = rules.upload_refusal(post.kind, len(post.slides), body)
        if refusal:
            raise PostError(refusal)
        if self.r2 is None:
            raise UpstreamError("Storage is not available")

        slide_id = uuid.uuid4().hex
        slide = Slide(
            slide_id=slide_id,
            media_type=body.media_type,
            content_type=body.content_type,
            r2_object_key=rules.slide_key(channel_id, post_id, slide_id, body.content_type),
            width=body.width,
            height=body.height,
            duration_seconds=body.duration_seconds,
            size_bytes=body.size_bytes,
        )
        if post.status == "scheduled":
            # The new slide cannot be uploaded yet; judge the post as it will be
            # once it is, so adding a slide to a scheduled post is possible at all.
            self._refuse_if_problems(
                post.model_copy(update={"slides": [*post.slides, slide.model_copy(update={"uploaded": True})]}),
                "add this slide to a scheduled post",
            )

        result = await self.db.posts.update_one(
            {"channel_id": channel_id, "post_id": post_id, "status": post.status},
            {"$push": {"slides": slide.model_dump()}, "$set": {"updated_at": now_ist()}},
        )
        if result.matched_count == 0:
            raise PostConflictError("The post changed while this request ran — reload and try again")

        upload_url = self.r2.generate_presigned_put_url(slide.r2_object_key, content_type=body.content_type)
        return SlideUploadOut(
            slide=self._slide_out(slide),
            upload_url=upload_url,
            upload_headers={"Content-Type": body.content_type},
        )

    async def complete_slide(self, channel_id: str, post_id: str, slide_id: str) -> PostOut:
        post = await self._load(channel_id, post_id)
        self._require(rules.can_edit_slides(post.status), post, "upload slides to")
        slide = next((s for s in post.slides if s.slide_id == slide_id), None)
        if slide is None:
            raise PostNotFoundError(f"Slide '{slide_id}' not found")
        if self.r2 is None:
            raise UpstreamError("Storage is not available")

        try:
            size = await asyncio.to_thread(self.r2.object_size, slide.r2_object_key)
        except Exception as exc:
            raise UpstreamError(f"Could not check the upload in storage: {exc}")
        if size is None:
            raise PostError("The file has not arrived in storage yet — upload it before completing")

        limit = rules.MAX_IMAGE_BYTES if slide.media_type == "image" else rules.MAX_VIDEO_BYTES
        if size > limit:
            # The declared size passed; the real one did not. Remove it so an
            # oversized object never reaches Instagram or lingers in the bucket.
            await self._delete_object(slide.r2_object_key)
            raise PostError(f"Uploaded file is {size} bytes; the limit for a {slide.media_type} is {limit} bytes")

        slides = [
            s.model_copy(update={"uploaded": True, "size_bytes": size}) if s.slide_id == slide_id else s
            for s in post.slides
        ]
        return await self._write(post, {"slides": [s.model_dump() for s in slides]})

    async def delete_slide(self, channel_id: str, post_id: str, slide_id: str) -> PostOut:
        post = await self._load(channel_id, post_id)
        self._require(rules.can_edit_slides(post.status), post, "remove slides from")
        slide = next((s for s in post.slides if s.slide_id == slide_id), None)
        if slide is None:
            raise PostNotFoundError(f"Slide '{slide_id}' not found")
        slides = [s for s in post.slides if s.slide_id != slide_id]
        if post.status == "scheduled":
            self._refuse_if_problems(
                post.model_copy(update={"slides": slides}), "remove this slide from a scheduled post"
            )
        out = await self._write(post, {"slides": [s.model_dump() for s in slides]})
        await self._delete_object(slide.r2_object_key)
        return out

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _fresh_run(scheduled_at: datetime) -> dict[str, Any]:
        """Fields that start a publish from scratch, forgetting any earlier attempt."""
        return {
            "status": "scheduled",
            "scheduled_at": scheduled_at,
            "attempts": 0,
            "last_error": None,
            "publish_state": None,
            "handoff_sent_at": None,
        }

    async def schedule(self, channel_id: str, post_id: str, scheduled_at: str) -> PostOut:
        post = await self._load(channel_id, post_id)
        self._require(rules.can_schedule(post.status), post, "schedule")
        when = parse_scheduled_at(scheduled_at)
        if when <= now_ist():
            raise PostError("scheduled_at must be in the future")
        self._refuse_if_problems(post, "schedule")
        return await self._write(post, self._fresh_run(when))

    async def unschedule(self, channel_id: str, post_id: str) -> PostOut:
        post = await self._load(channel_id, post_id)
        self._require(rules.can_unschedule(post.status), post, "unschedule")
        return await self._write(post, {"status": "draft", "scheduled_at": None, "handoff_sent_at": None})

    async def publish_now(self, channel_id: str, post_id: str) -> PostOut:
        post = await self._load(channel_id, post_id)
        self._require(rules.can_publish_now(post.status), post, "publish")
        self._refuse_if_problems(post, "publish")
        out = await self._write(post, self._fresh_run(now_ist()))
        self._wake()
        return out

    async def retry(self, channel_id: str, post_id: str) -> PostOut:
        post = await self._load(channel_id, post_id)
        self._require(rules.can_retry(post.status), post, "retry")
        self._refuse_if_problems(post, "retry")
        out = await self._write(post, self._fresh_run(now_ist()))
        self._wake()
        return out

    async def mark_published(self, channel_id: str, post_id: str, body: MarkPublishedRequest) -> PostOut:
        post = await self._load(channel_id, post_id)
        self._require(rules.can_mark_published(post.status), post, "mark as published")
        permalink = (body.permalink or "").strip() or None
        return await self._write(post, {"status": "published", "published_at": now_ist(), "permalink": permalink})

    async def archive(self, channel_id: str, post_id: str) -> PostOut:
        post = await self._load(channel_id, post_id)
        self._require(rules.can_archive(post.status), post, "archive")
        return await self._write(post, {"status": "archived", "archived_from_status": post.status})

    async def restore(self, channel_id: str, post_id: str) -> PostOut:
        post = await self._load(channel_id, post_id)
        self._require(post.status == "archived", post, "restore")
        target = rules.restore_status(post.archived_from_status, post.scheduled_at, now_ist())
        fields: dict[str, Any] = {"status": target, "archived_from_status": None}
        if target == "draft" and post.archived_from_status == "scheduled":
            fields["scheduled_at"] = None
        return await self._write(post, fields)

    async def handoff(self, channel_id: str, post_id: str) -> HandoffOut:
        post = await self._load(channel_id, post_id)
        if self.r2 is None:
            raise UpstreamError("Storage is not available")
        slides = [
            HandoffSlide(
                slide_id=s.slide_id,
                media_type=s.media_type,
                content_type=s.content_type,
                filename=rules.slide_filename(post.post_id, i, s),
                # 24 h: the owner may open the email hours after it arrives.
                download_url=self.r2.generate_presigned_url(s.r2_object_key, expires_in=HANDOFF_URL_TTL),
            )
            for i, s in enumerate(post.slides)
            if s.uploaded
        ]
        return HandoffOut(
            post_id=post.post_id,
            channel_id=post.channel_id,
            kind=post.kind,
            caption=post.caption,
            first_comment=post.first_comment,
            music_note=post.music_note,
            status=post.status,
            scheduled_at=to_ist_iso(post.scheduled_at),
            permalink=post.permalink,
            slides=slides,
        )

    # ------------------------------------------------------------------
    # Live Instagram reads
    # ------------------------------------------------------------------

    async def _instagram(self, channel_id: str) -> tuple[Any, str]:
        channel = await self._channel(channel_id)
        ig_user_id = channel.get("instagram_user_id") or ""
        service = await self.instagram_manager.get_service(channel_id) if self.instagram_manager else None
        if service is None or not ig_user_id:
            raise PostError("This channel is not connected to Instagram")
        return service, str(ig_user_id)

    async def publishing_limit(self, channel_id: str) -> PublishingLimitOut:
        service, ig_user_id = await self._instagram(channel_id)
        try:
            limit = await asyncio.to_thread(service.get_publishing_limit, ig_user_id)
        except Exception as exc:
            raise UpstreamError(f"Instagram did not return the publishing limit: {exc}")
        return PublishingLimitOut(
            quota_total=limit["quota_total"],
            quota_usage=limit["quota_usage"],
            quota_duration_seconds=limit["quota_duration"],
        )

    async def feed(self, channel_id: str, limit: int, after: str | None, include_reels: bool) -> FeedPage:
        service, ig_user_id = await self._instagram(channel_id)
        limit = max(1, min(limit, MAX_FEED_LIMIT))
        try:
            page = await asyncio.to_thread(service.get_media_page, ig_user_id, limit=limit, after=after)
        except Exception as exc:
            raise UpstreamError(f"Instagram did not return the feed: {exc}")

        media = [m for m in page["data"] if include_reels or m.get("media_product_type") != "REELS"]
        ids = [str(m.get("id")) for m in media if m.get("id")]
        ours: dict[str, str] = {}
        if ids:
            docs = await self.db.posts.find(
                {"channel_id": channel_id, "instagram_media_id": {"$in": ids}},
                {"post_id": 1, "instagram_media_id": 1},
            ).to_list(length=None)
            ours = {d["instagram_media_id"]: d["post_id"] for d in docs}
        return FeedPage(items=[feed_item(m, ours) for m in media], next_cursor=page.get("next_cursor"))

    async def insights(self, channel_id: str, media_id: str) -> MediaInsightsOut:
        service, _ = await self._instagram(channel_id)
        try:
            values, unavailable = await asyncio.to_thread(service.get_media_insights, media_id, INSIGHT_METRICS)
        except Exception as exc:
            raise UpstreamError(f"Instagram did not return insights: {exc}")
        return MediaInsightsOut(media_id=media_id, metrics=values, unavailable=unavailable)


def feed_item(media: dict[str, Any], ours: dict[str, str]) -> FeedItem:
    """Shape one Graph media object for the UI."""
    media_type = str(media.get("media_type") or "")
    children_raw = (media.get("children") or {}).get("data") or []
    children = [
        FeedChild(media_type=str(c.get("media_type") or ""), url=c.get("media_url") or c.get("thumbnail_url"))
        for c in children_raw
    ]
    if media_type == "VIDEO":
        thumb = media.get("thumbnail_url")
    else:
        # A carousel's own media_url is its first slide; fall back to that
        # child in case the API leaves it out.
        thumb = media.get("media_url") or (children[0].url if children else None)
    media_id = str(media.get("id") or "")
    return FeedItem(
        instagram_media_id=media_id,
        media_type=media_type,
        media_product_type=media.get("media_product_type"),
        caption=str(media.get("caption") or ""),
        permalink=media.get("permalink"),
        timestamp=to_ist_iso(media.get("timestamp")),
        thumbnail_url=thumb,
        like_count=int(media.get("like_count") or 0),
        comments_count=int(media.get("comments_count") or 0),
        children=children,
        post_id=ours.get(media_id),
    )
