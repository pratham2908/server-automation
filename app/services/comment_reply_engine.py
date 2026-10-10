from __future__ import annotations

"""Comment reply engine -- fetches unreplied positive comments and auto-replies.

Entry-point: ``run_comment_reply_cycle(channel_id, ...)``
"""

import random
from datetime import timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.comment_reply_review import (
    MODE_REVIEW,
    build_pending_row,
    is_own_comment,
    is_reviewable,
    normalise_mode,
    own_identities,
)
from app.services.comment_reply_text import video_context
from app.services.gemini import GeminiService
from app.services.instagram import InstagramService
from app.services.instagram_tokens import PREFER_FOR_READING_COMMENTS, PREFER_FOR_REPLYING
from app.services.youtube import YouTubeService
from app.timezone import now_ist

logger = get_logger(__name__)

_DEFAULT_TEMPLATES = [
    "Thanks so much! Subscribe so you don't miss more content like this!",
    "Glad you enjoyed it! Hit subscribe for more!",
    "Thank you! Don't forget to subscribe for more!",
]
_DEFAULT_MAX_REPLIES = 50
_DEFAULT_MAX_VIDEOS = 10
_DEFAULT_RECENCY_DAYS = 30
_SENTIMENT_BATCH_SIZE = 50


async def _get_reply_config(db: AsyncIOMotorDatabase) -> dict[str, Any]:
    doc = await db.config.find_one({"key": "comment_reply_config"})
    if not doc:
        return {
            "enabled": True,
            "reply_templates": _DEFAULT_TEMPLATES,
            "max_replies_per_run": _DEFAULT_MAX_REPLIES,
            "max_videos_per_run": _DEFAULT_MAX_VIDEOS,
            "video_recency_days": _DEFAULT_RECENCY_DAYS,
        }
    from typing import cast

    return cast(dict[str, Any], doc)


def _pick_template(templates: list[str]) -> str:
    return random.choice(templates) if templates else _DEFAULT_TEMPLATES[0]


async def run_comment_reply_cycle(
    channel_id: str,
    db: AsyncIOMotorDatabase,
    youtube_service_manager: Any,
    instagram_service_manager: Any,
    gemini_service: GeminiService,
) -> dict[str, Any]:
    """Execute one reply cycle for *channel_id*.

    1. Load config & channel doc
    2. Fetch recent published videos
    3. For each video: fetch comments, exclude own + already-replied, classify, reply
    """
    stats: dict[str, Any] = {"replied": 0, "drafted": 0, "skipped": 0, "errors": 0, "videos_processed": 0}

    config = await _get_reply_config(db)
    if not config.get("enabled", True):
        logger.info("Comment reply system is disabled — skipping channel %s", channel_id)
        return stats

    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        logger.warning("Channel %s not found", channel_id)
        return stats

    platform = channel.get("platform", "youtube")
    review_mode = normalise_mode(channel.get("comment_reply_mode")) == MODE_REVIEW
    templates = config.get("reply_templates", _DEFAULT_TEMPLATES)
    max_replies = config.get("max_replies_per_run", _DEFAULT_MAX_REPLIES)
    max_videos = config.get("max_videos_per_run", _DEFAULT_MAX_VIDEOS)
    recency_days = config.get("video_recency_days", _DEFAULT_RECENCY_DAYS)

    yt_svc: YouTubeService | None = None
    ig_svc: InstagramService | None = None
    ig_reply_svc: InstagramService | None = None

    if platform == "youtube" and youtube_service_manager:
        yt_svc = await youtube_service_manager.get_service(channel_id)
    elif platform == "instagram" and instagram_service_manager:
        ig_svc = await instagram_service_manager.get_service(channel_id, PREFER_FOR_READING_COMMENTS)
        ig_reply_svc = await instagram_service_manager.get_service(channel_id, PREFER_FOR_REPLYING)

    if not yt_svc and not ig_svc:
        logger.warning("No platform service available for channel %s", channel_id)
        return stats

    cutoff = now_ist() - timedelta(days=recency_days)
    videos = (
        await db.videos.find(
            {
                "channel_id": channel_id,
                "status": "published",
                "published_at": {"$gte": cutoff},
            }
        )
        .sort("published_at", -1)
        .limit(max_videos)
        .to_list(length=max_videos)
    )

    logger.info("Found %d videos for comment-reply cycle on channel '%s'", len(videos), channel_id)

    own_yt_channel_id = channel.get("youtube_channel_id", "")
    own_ig_identities = own_identities(channel)

    total_replies = 0

    for video in videos:
        if total_replies >= max_replies:
            break

        stats["videos_processed"] += 1
        platform_vid_id = video.get("youtube_video_id") if platform == "youtube" else video.get("instagram_media_id")
        if not platform_vid_id:
            continue

        try:
            if platform == "youtube" and yt_svc:
                raw_comments = yt_svc.get_video_comments(platform_vid_id, max_comments=200)
            elif platform == "instagram" and ig_svc:
                raw_comments = ig_svc.get_media_comments(platform_vid_id)
            else:
                continue
        except Exception as exc:
            logger.warning("Failed to fetch comments for video %s: %s", platform_vid_id, exc)
            stats["errors"] += 1
            continue

        if not raw_comments:
            continue

        # Instagram only reports who wrote a comment with instagram_manage_comments. Without it every
        # author is blank, so our own comments cannot be told apart and would be replied to.
        if platform == "instagram" and not any(c.get("author") for c in raw_comments):
            logger.warning(
                "Instagram comments on %s carry no author: channel '%s' is missing instagram_manage_comments; "
                "reconnect it to grant the permission",
                platform_vid_id,
                channel_id,
            )

        # Filter out own comments
        filtered: list[dict[str, Any]] = []
        for c in raw_comments:
            if platform == "youtube":
                if c.get("author_channel_id") == own_yt_channel_id:
                    continue
            elif is_own_comment(c.get("author", ""), own_ig_identities):
                continue
            filtered.append(c)

        if not filtered:
            continue

        # Filter out already-replied comment IDs
        comment_ids = [c["comment_id"] for c in filtered if c.get("comment_id")]
        already_replied = set()
        if comment_ids:
            existing = await db.comment_replies.find(
                {"channel_id": channel_id, "comment_id": {"$in": comment_ids}},
                {"comment_id": 1},
            ).to_list(length=None)
            from typing import cast

            already_replied = {cast(str, d["comment_id"]) for d in existing}

        candidates = [c for c in filtered if c.get("comment_id") and c["comment_id"] not in already_replied]
        if not candidates:
            continue

        context = video_context(video)

        # Classify sentiment in batches
        # (comment, sentiment) pairs that get a reply or a draft. Auto mode handles only positive ones;
        # review mode also drafts negative and neutral, since a person approves each before it is sent.
        to_answer: list[tuple[dict[str, Any], str]] = []
        sentiment_counts = {"positive": 0, "negative": 0, "neutral": 0, "spam": 0}

        for i in range(0, len(candidates), _SENTIMENT_BATCH_SIZE):
            batch = candidates[i : i + _SENTIMENT_BATCH_SIZE]
            try:
                results = await gemini_service.classify_comment_sentiment(batch, video_context=context)
            except Exception as exc:
                logger.warning("Gemini sentiment classification failed: %s", exc)
                stats["errors"] += 1
                continue

            sentiment_map = {r["comment_id"]: r.get("sentiment", "") for r in results if isinstance(r, dict)}
            for c in batch:
                sent = sentiment_map.get(c["comment_id"], "neutral")
                sentiment_counts[sent] = sentiment_counts.get(sent, 0) + 1
                if sent == "positive" or (review_mode and is_reviewable(sent)):
                    to_answer.append((c, sent))
                else:
                    # Mark as skipped in DB to avoid re-analysis
                    status_val = f"skipped_{sent}" if sent in ("negative", "neutral", "spam") else "skipped_other"
                    await db.comment_replies.insert_one(
                        {
                            "channel_id": channel_id,
                            "video_id": video.get("video_id", ""),
                            "video_title": video.get("title", ""),
                            "video_url": c.get("video_url", ""),
                            "platform": platform,
                            "comment_id": c["comment_id"],
                            "comment_text": c.get("text", ""),
                            "comment_author": c.get("author", ""),
                            "comment_url": c.get("comment_url", ""),
                            "sentiment": sent,
                            "status": status_val,
                            "replied_at": now_ist(),  # processed_at
                        }
                    )

        logger.info(
            "Video '%s': %d total comments, %d positive, %d negative, %d neutral, %d spam",
            video.get("title", platform_vid_id)[:50],
            len(candidates),
            sentiment_counts["positive"],
            sentiment_counts["negative"],
            sentiment_counts["neutral"],
            sentiment_counts["spam"],
        )

        # Reply to (or, in review mode, draft a reply for) each answerable comment
        for c, sent in to_answer:
            if total_replies >= max_replies:
                break

            # AI-generated reply first
            reply_text = ""
            try:
                reply_text = await gemini_service.generate_comment_reply(
                    comment_text=c.get("text", ""),
                    video_title=video.get("title", ""),
                    platform=platform,
                    sentiment=sent,
                    video_context=context,
                )
            except Exception as exc:
                logger.warning("AI reply generation failed: %s", exc)

            if not reply_text:
                if sent != "positive":
                    # The canned templates are subscribe pitches, wrong for a complaint or a question.
                    # Leave it unrecorded so the next cycle tries again rather than queueing a bad draft.
                    stats["errors"] += 1
                    continue
                reply_text = _pick_template(templates)

            if review_mode:
                await db.comment_replies.insert_one(
                    build_pending_row(
                        channel_id=channel_id,
                        platform=platform,
                        video=video,
                        comment=c,
                        sentiment=sent,
                        suggested_reply=reply_text,
                    )
                )
                total_replies += 1
                stats["drafted"] += 1
                continue

            try:
                if platform == "youtube" and yt_svc:
                    reply_id = yt_svc.reply_to_comment(c["comment_id"], reply_text)
                elif platform == "instagram" and ig_reply_svc:
                    reply_id = ig_reply_svc.reply_to_comment(c["comment_id"], reply_text)
                else:
                    continue
            except Exception as exc:
                logger.warning("Failed to reply to comment %s: %s", c["comment_id"], exc)
                stats["errors"] += 1
                continue

            await db.comment_replies.insert_one(
                {
                    "channel_id": channel_id,
                    "video_id": video.get("video_id", ""),
                    "video_title": video.get("title", ""),
                    "video_url": c.get("video_url", ""),
                    "platform": platform,
                    "comment_id": c["comment_id"],
                    "comment_text": c.get("text", ""),
                    "comment_author": c.get("author", ""),
                    "comment_url": c.get("comment_url", ""),
                    "sentiment": "positive",
                    "status": "replied",
                    "reply_text": reply_text,
                    "reply_id": reply_id,
                    "replied_at": now_ist(),
                }
            )
            total_replies += 1
            stats["replied"] += 1

        stats["skipped"] += len(candidates) - len(to_answer)

    logger.info(
        "Comment reply cycle for '%s': %d replies, %d drafted, %d skipped, %d errors",
        channel_id,
        stats["replied"],
        stats["drafted"],
        stats["skipped"],
        stats["errors"],
    )
    return stats
