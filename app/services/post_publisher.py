"""Background publisher for Instagram image / carousel / story posts.

Instagram has no native scheduling and its containers expire after 24 hours, so
containers are created at publish time, here. Posts get their own worker rather
than riding the reel poller so a slow carousel can never delay a reel.

A publish is a resumable state machine, not one long call. Each tick creates
whatever containers are missing, reads their status once, and returns; every
container id is persisted in ``publish_state`` the moment Instagram hands it
back. A restart or a video that takes minutes to process therefore resumes on
the next tick instead of creating a duplicate post. Every Graph call goes
through ``asyncio.to_thread`` — the requests are synchronous.

Posts with ``music_mode="in_app"`` are never published by the API (it cannot add
a song). Half an hour before their time the owner is emailed a link to the
handoff page and finishes the post in the Instagram app; we then spot it in the
account's feed by caption, or the owner marks it published.
"""

from __future__ import annotations

import asyncio
import html
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from app.config import Settings, get_settings
from app.database import is_channel_paused
from app.logger import get_logger
from app.models.post import ChildContainer, PostDoc, PublishState, Slide
from app.services import post_rules as rules
from app.services.email_service import resolve_owner_email, send_email
from app.services.errors import get_error_service
from app.timezone import now_ist, to_ist_iso

logger = get_logger(__name__)

_TICK_SECONDS = 60
MAX_ATTEMPTS = 5
# Instagram fetches the media while the container processes; six hours covers a
# slow video with room to spare, well inside the 2 h stuck limit twice over.
MEDIA_URL_TTL = 6 * 3600
# The manual-post detector reads the feed at most this often per channel: every
# tick would spend ~60 Graph calls an hour per channel on a best-effort nicety.
DETECT_INTERVAL = timedelta(minutes=5)
DETECT_MEDIA_COUNT = 25

_FEATURE = "Instagram Post Publisher"

# Created inside the running loop (see run_post_publisher): an Event made at
# import time could end up bound to a different loop than the one waiting on it.
_wake: asyncio.Event | None = None
_last_detect: dict[str, datetime] = {}


def wake_post_publisher() -> None:
    """Start the next tick now instead of at the end of the current wait."""
    if _wake is not None:
        _wake.set()


class ContainerFailedError(Exception):
    """Instagram rejected a container for good (ERROR / EXPIRED). Retrying the same media will not help."""


# ------------------------------------------------------------------
# Loop
# ------------------------------------------------------------------


async def run_post_publisher(db: Any, r2_service: Any) -> None:
    global _wake
    _wake = asyncio.Event()
    logger.info("[Posts] Publisher started (tick %ds)", _TICK_SECONDS)

    while True:
        try:
            from app.main import instagram_service_manager  # type: ignore[import]

            await run_tick(db, r2_service, instagram_service_manager, get_settings(), now_ist())
        except asyncio.CancelledError:
            logger.info("[Posts] Publisher shutting down")
            break
        except Exception as exc:
            logger.exception("[Posts] Publisher tick failed")
            await get_error_service(db).log_error(
                feature=f"{_FEATURE} Loop",
                message="Post publisher tick failed",
                exception=exc,
            )

        try:
            await asyncio.wait_for(_wake.wait(), timeout=_TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
        _wake.clear()


async def run_tick(db: Any, r2: Any, instagram_manager: Any, settings: Settings, now: datetime) -> None:
    """One pass: hand-offs, then manual-post detection, then API publishing.

    The phases are independent, so one failing is logged and the others still run.
    """
    for phase, call in (
        ("hand-offs", lambda: send_handoffs(db, settings, now)),
        ("manual-post detection", lambda: detect_manual_posts(db, instagram_manager, now, _last_detect)),
        ("publishing", lambda: publish_due_posts(db, r2, instagram_manager, now)),
    ):
        try:
            await call()
        except Exception as exc:
            logger.exception("[Posts] %s phase failed", phase)
            await get_error_service(db).log_error(
                feature=_FEATURE,
                message=f"Post publisher {phase} phase failed",
                exception=exc,
            )


async def _channel(db: Any, channel_id: str) -> dict[str, Any] | None:
    doc = await db.channels.find_one({"channel_id": channel_id})
    return dict(doc) if doc else None


# ------------------------------------------------------------------
# 1. Hand-offs (music_mode="in_app")
# ------------------------------------------------------------------


def build_handoff_email(post: PostDoc, channel_name: str, username: str | None, link: str) -> tuple[str, str, str]:
    """(subject, text, html) for the "time to post" email. Pure.

    The account leads the subject: Instagram shares into whichever account the
    app last had open and nothing outside the app can choose it, so the owner
    has to switch by hand — the one thing they must see before tapping.
    """
    what = {"image": "image post", "carousel": "carousel", "story": "story"}[post.kind]
    when = to_ist_iso(post.scheduled_at) or "now"
    account = f"@{username}" if username else channel_name
    subject = f"Post on {account}: {what} for {channel_name}"
    lines = [
        f"Post this as {account} — switch to that account in Instagram before you share.",
        f"Your {what} for {channel_name} is due at {when}.",
        "Instagram's API can't add a song, so this one is finished in the app.",
        "",
    ]
    if post.music_note:
        lines.append(f"Song: {post.music_note}")
    lines += [f"Open the handoff page: {link}", "", "Caption:", post.caption or "(none)"]
    text = "\n".join(lines)

    song = f"<p><strong>Song:</strong> {html.escape(post.music_note)}</p>" if post.music_note else ""
    body = (
        f'<p style="font-size:18px"><strong>Post as {html.escape(account)}</strong> — switch to that account in '
        "Instagram before you share.</p>"
        f"<p>Your {what} for <strong>{html.escape(channel_name)}</strong> is due at {html.escape(when)}.</p>"
        "<p>Instagram's API can't add a song, so this one is finished in the app.</p>"
        f"{song}"
        f'<p><a href="{html.escape(link, quote=True)}">Open the handoff page</a></p>'
        f'<p><strong>Caption</strong></p><pre style="white-space:pre-wrap">{html.escape(post.caption or "(none)")}</pre>'
    )
    return subject, text, body


async def send_handoffs(db: Any, settings: Settings, now: datetime) -> int:
    """Email the owner for every in-app post whose time is within the lead window."""
    docs = await db.posts.find(
        {
            "music_mode": "in_app",
            "status": "scheduled",
            "scheduled_at": {"$lte": now + rules.HANDOFF_LEAD},
        }
    ).to_list(length=None)

    sent = 0
    for doc in docs:
        post = PostDoc.model_validate(doc)
        if not rules.handoff_due(post.scheduled_at, now):
            continue
        channel = await _channel(db, post.channel_id)
        if channel is None or is_channel_paused(channel):
            continue

        # Claim first, email second: the status flip is what guarantees the
        # owner gets one email, even if two ticks overlap or the send is slow.
        claimed = await db.posts.update_one(
            {"post_id": post.post_id, "status": "scheduled", "music_mode": "in_app"},
            {"$set": {"status": "awaiting_manual", "handoff_sent_at": now, "updated_at": now}},
        )
        if claimed.matched_count == 0:
            continue

        # Channel ids can contain spaces ("scroll and tell"), which break a bare link in mail clients.
        link = f"{settings.ANALYZER_PUBLIC_URL.rstrip('/')}/handoff/{quote(post.channel_id)}/{post.post_id}"
        subject, text, body = build_handoff_email(
            post, str(channel.get("name") or post.channel_id), channel.get("instagram_username"), link
        )
        recipient = await resolve_owner_email(db, settings)
        if not await send_email(settings, recipient, subject, text, body):
            # The post still shows as "awaiting manual" in the app, which is the
            # fallback; the email is a nudge, so it is not retried.
            logger.warning("[Posts] Hand-off email for post %s was not sent", post.post_id)
        else:
            sent += 1
        logger.info("[Posts] Handed off post %s (channel %s) for manual posting", post.post_id, post.channel_id)
    return sent


# ------------------------------------------------------------------
# 2. Detect posts the owner finished in the app
# ------------------------------------------------------------------


async def detect_manual_posts(
    db: Any,
    instagram_manager: Any,
    now: datetime,
    last_checked: dict[str, datetime],
) -> int:
    docs = await db.posts.find({"status": "awaiting_manual"}).to_list(length=None)
    by_channel: dict[str, list[PostDoc]] = defaultdict(list)
    for doc in docs:
        by_channel[doc["channel_id"]].append(PostDoc.model_validate(doc))

    matched = 0
    for channel_id, posts in by_channel.items():
        previous = last_checked.get(channel_id)
        if previous is not None and now - previous < DETECT_INTERVAL:
            continue
        channel = await _channel(db, channel_id)
        if channel is None or is_channel_paused(channel) or instagram_manager is None:
            continue
        service = await instagram_manager.get_service(channel_id)
        ig_user_id = channel.get("instagram_user_id")
        if service is None or not ig_user_id:
            continue
        last_checked[channel_id] = now

        try:
            page = await asyncio.to_thread(service.get_media_page, ig_user_id, limit=DETECT_MEDIA_COUNT)
        except Exception as exc:
            # Best effort: the owner can always tap "I've posted it", and the
            # next check comes round in a few minutes.
            logger.warning("[Posts] Could not read the feed of channel %s to detect manual posts: %s", channel_id, exc)
            continue

        media: list[dict[str, Any]] = page["data"]
        ids = [str(m["id"]) for m in media if m.get("id")]
        linked_docs = await db.posts.find(
            {"instagram_media_id": {"$in": ids}},
            {"instagram_media_id": 1},
        ).to_list(length=None)
        linked = {str(d["instagram_media_id"]) for d in linked_docs}

        for post in sorted(posts, key=lambda p: p.handoff_sent_at or now):
            item = rules.match_manual_media(post, media, linked)
            if item is None:
                continue
            media_id = str(item["id"])
            published_at = rules.media_time(item) or now
            result = await db.posts.update_one(
                {"post_id": post.post_id, "status": "awaiting_manual"},
                {
                    "$set": {
                        "status": "published",
                        "instagram_media_id": media_id,
                        "permalink": item.get("permalink"),
                        "published_at": published_at,
                        "updated_at": now,
                    }
                },
            )
            if result.matched_count:
                linked.add(media_id)
                matched += 1
                logger.info("[Posts] Detected manual post %s as Instagram media %s", post.post_id, media_id)
    return matched


# ------------------------------------------------------------------
# 3. Publish through the API (music_mode="none")
# ------------------------------------------------------------------


async def publish_due_posts(db: Any, r2: Any, instagram_manager: Any, now: datetime) -> None:
    docs = (
        await db.posts.find(
            {
                "music_mode": {"$ne": "in_app"},
                "$or": [
                    {"status": "scheduled", "scheduled_at": {"$lte": now}},
                    {"status": "publishing"},
                ],
            }
        )
        .sort("scheduled_at", 1)
        .to_list(length=None)
    )

    for doc in docs:
        post = PostDoc.model_validate(doc)
        channel = await _channel(db, post.channel_id)
        if channel is None:
            await _fail(db, post, "The channel no longer exists", log=False)
            continue
        if is_channel_paused(channel):
            # Paused is temporary: the post must still be waiting on resume.
            logger.info("[Posts] Channel %s is paused — leaving post %s", post.channel_id, post.post_id)
            continue

        if post.status == "scheduled":
            claimed = await _claim(db, post, now)
            if claimed is None:
                continue
            post = claimed

        await advance_publish(db, r2, instagram_manager, channel, post, now)


async def _claim(db: Any, post: PostDoc, now: datetime) -> PostDoc | None:
    """scheduled → publishing, atomically. ``None`` when someone else moved it first or it cannot publish."""
    problems = rules.validate(post).problems
    if problems:
        await db.posts.update_one(
            {"post_id": post.post_id, "status": "scheduled"},
            {"$set": {"status": "failed", "last_error": "; ".join(problems), "updated_at": now}},
        )
        return None

    state = PublishState(started_at=now)
    result = await db.posts.update_one(
        {"post_id": post.post_id, "status": "scheduled"},
        {"$set": {"status": "publishing", "publish_state": state.model_dump(), "updated_at": now}},
    )
    if result.matched_count == 0:
        return None
    return post.model_copy(update={"status": "publishing", "publish_state": state})


async def _save_state(db: Any, post: PostDoc, state: PublishState, now: datetime) -> None:
    await db.posts.update_one(
        {"post_id": post.post_id, "status": "publishing"},
        {"$set": {"publish_state": state.model_dump(), "updated_at": now}},
    )


async def _fail(db: Any, post: PostDoc, reason: str, *, log: bool = True) -> None:
    await db.posts.update_one(
        {"post_id": post.post_id, "status": post.status},
        {"$set": {"status": "failed", "last_error": reason, "updated_at": now_ist()}},
    )
    logger.error("[Posts] Post %s (channel %s) failed: %s", post.post_id, post.channel_id, reason)
    if log:
        await get_error_service(db).log_error(
            feature=_FEATURE,
            message=f"Failed to publish {post.kind} post: {reason}",
            context={"post_id": post.post_id, "channel_id": post.channel_id},
        )


async def advance_publish(
    db: Any,
    r2: Any,
    instagram_manager: Any,
    channel: dict[str, Any],
    post: PostDoc,
    now: datetime,
) -> None:
    """Move one ``publishing`` post as far as Instagram allows this tick."""
    state = post.publish_state or PublishState(started_at=now)
    if rules.is_stuck(state.started_at, now):
        await _fail(db, post, "Instagram never finished processing")
        return

    try:
        service = await instagram_manager.get_service(post.channel_id) if instagram_manager else None
        if service is None:
            raise RuntimeError("No Instagram connection for this channel")
        ig_user_id = str(channel.get("instagram_user_id") or "")
        if not ig_user_id:
            raise RuntimeError("Channel has no instagram_user_id")
        await _step(db, r2, service, ig_user_id, post, state, now)
    except ContainerFailedError as exc:
        await _fail(db, post, str(exc))
    except Exception as exc:
        attempts = post.attempts + 1
        logger.warning(
            "[Posts] Publish step for %s failed (attempt %d/%d): %s", post.post_id, attempts, MAX_ATTEMPTS, exc
        )
        if attempts >= MAX_ATTEMPTS:
            await db.posts.update_one(
                {"post_id": post.post_id, "status": "publishing"},
                {"$set": {"status": "failed", "attempts": attempts, "last_error": str(exc)[:500], "updated_at": now}},
            )
            await get_error_service(db).log_error(
                feature=_FEATURE,
                message=f"Failed to publish {post.kind} post after {attempts} attempts",
                exception=exc,
                context={"post_id": post.post_id, "channel_id": post.channel_id},
            )
        else:
            # Containers made so far stay in publish_state, so the retry picks up
            # where this attempt stopped.
            await db.posts.update_one(
                {"post_id": post.post_id, "status": "publishing"},
                {"$set": {"attempts": attempts, "last_error": str(exc)[:500], "updated_at": now}},
            )


def _media_url(r2: Any, slide: Slide) -> str:
    return str(r2.generate_presigned_url(slide.r2_object_key, expires_in=MEDIA_URL_TTL))


async def _check(service: Any, container_id: str, what: str) -> bool:
    """Whether the container is FINISHED. Raises when it never will be."""
    code, detail = await asyncio.to_thread(service.get_container_status, container_id)
    if code in rules.TERMINAL_CONTAINER_CODES:
        reason = f": {detail}" if detail else ""
        raise ContainerFailedError(f"Instagram could not process {what} ({code}{reason})")
    if code == "PUBLISHED":
        # media_publish went through on an earlier tick but its reply was lost,
        # so the media id is unknown. Publishing again would fail anyway; say
        # what happened rather than guess which media it was.
        raise ContainerFailedError(
            "Instagram reports this post as already published, but the reply was lost — check the account"
        )
    return code == "FINISHED"


async def _step(
    db: Any,
    r2: Any,
    service: Any,
    ig_user_id: str,
    post: PostDoc,
    state: PublishState,
    now: datetime,
) -> None:
    if post.kind == "carousel":
        known = {c.slide_id: c for c in state.children if not rules.container_expired(c.created_at, now)}
        for slide in post.slides:
            if slide.slide_id in known:
                continue
            url = _media_url(r2, slide)
            if slide.media_type == "image":
                container_id = await asyncio.to_thread(
                    service.create_image_container,
                    ig_user_id,
                    url,
                    is_carousel_item=True,
                    alt_text=slide.alt_text,
                )
            else:
                container_id = await asyncio.to_thread(service.create_carousel_video_item, ig_user_id, url)
            known[slide.slide_id] = ChildContainer(slide_id=slide.slide_id, container_id=container_id, created_at=now)
            # Saved after every creation: a crash between two slides must not
            # recreate the first one on the next tick.
            state.children = list(known.values())
            await _save_state(db, post, state, now)
        # Slide order is the carousel order, whatever order the containers were made in.
        children = [known[s.slide_id] for s in post.slides]
        state.children = children

        for n, child in enumerate(children, start=1):
            if not child.finished:
                child.finished = await _check(service, child.container_id, f"slide {n}")
        await _save_state(db, post, state, now)
        if not all(c.finished for c in children):
            return

        if state.container_id is None or rules.container_expired(state.container_created_at, now):
            state.container_id = await asyncio.to_thread(
                service.create_carousel_container,
                ig_user_id,
                [c.container_id for c in children],
                post.caption,
            )
            state.container_created_at = now
            await _save_state(db, post, state, now)

    elif state.container_id is None or rules.container_expired(state.container_created_at, now):
        slide = post.slides[0]
        url = _media_url(r2, slide)
        if post.kind == "story":
            kwargs = {"image_url": url} if slide.media_type == "image" else {"video_url": url}
            state.container_id = await asyncio.to_thread(service.create_story_container, ig_user_id, **kwargs)
        else:
            state.container_id = await asyncio.to_thread(
                service.create_image_container,
                ig_user_id,
                url,
                caption=post.caption,
                alt_text=slide.alt_text,
            )
        state.container_created_at = now
        await _save_state(db, post, state, now)

    container_id = str(state.container_id)
    if not await _check(service, container_id, "the post"):
        return

    media_id = await asyncio.to_thread(service.publish_container, ig_user_id, container_id)
    # Recorded before anything else can fail: from here the post is live, and
    # nothing below may send it back through the publish path.
    await db.posts.update_one(
        {"post_id": post.post_id, "status": "publishing"},
        {
            "$set": {
                "status": "published",
                "instagram_media_id": media_id,
                "published_at": now,
                "last_error": None,
                "publish_state": state.model_dump(),
                "updated_at": now,
            }
        },
    )
    logger.success("[Posts] Published %s post %s as media %s", post.kind, post.post_id, media_id)

    await _after_publish(db, service, post, media_id, now)


async def _after_publish(db: Any, service: Any, post: PostDoc, media_id: str, now: datetime) -> None:
    """Permalink and first comment: niceties on a post that is already live."""
    fields: dict[str, Any] = {}
    try:
        fields["permalink"] = await asyncio.to_thread(service.get_permalink, media_id)
    except Exception as exc:
        # The post is live; a missing link only costs the UI a button.
        logger.warning("[Posts] Could not fetch the permalink of media %s: %s", media_id, exc)

    comment = (post.first_comment or "").strip()
    if comment:
        # Tried once, unlike reels: a post's comment has no sweep to retry it,
        # and the outcome is shown on the post either way.
        try:
            await asyncio.to_thread(service.post_comment, media_id, comment)
            fields["first_comment_status"] = "posted"
        except Exception as exc:
            logger.warning("[Posts] First comment on media %s failed: %s", media_id, exc)
            fields["first_comment_status"] = "failed"

    if fields:
        fields["updated_at"] = now
        await db.posts.update_one({"post_id": post.post_id}, {"$set": fields})
