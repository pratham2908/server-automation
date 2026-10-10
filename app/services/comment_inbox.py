"""The comment inbox: read threads, draft a reply with the whole thread, send one.

The platform clients are passed in (``reader`` to read, ``poster`` to write),
because on Instagram they can be different tokens: Facebook Login reads comments
and Instagram Login writes them. Both are duck-typed so the logic is testable
without either API.
"""

from __future__ import annotations

import asyncio
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.comment_reply_review import STATUS_PENDING, STATUS_REPLIED, own_identities, validate_reply
from app.services.comment_reply_text import video_context
from app.services.comment_threads import (
    PLATFORM_YOUTUBE,
    build_thread,
    default_target,
    find_message,
    make_message,
    sort_threads,
    thread_transcript,
    with_mention,
)
from app.timezone import now_ist


class ThreadNotFoundError(Exception):
    """The thread (or the message inside it) is not on the platform any more."""


def platform_video_id(video: dict[str, Any], platform: str) -> str:
    key = "youtube_video_id" if platform == PLATFORM_YOUTUBE else "instagram_media_id"
    return str(video.get(key) or "")


def _shape(raw: dict[str, Any], channel: dict[str, Any], video: dict[str, Any], platform: str) -> dict[str, Any]:
    identities = own_identities(channel)
    own_yt = str(channel.get("youtube_channel_id") or "")

    def msg(r: dict[str, Any]) -> dict[str, Any]:
        return make_message(platform, r, own_identities=identities, own_youtube_channel_id=own_yt)

    return build_thread(
        msg(raw["top"]),
        [msg(r) for r in raw["replies"]],
        video_id=str(video.get("video_id", "")),
        platform=platform,
        comment_url=raw.get("comment_url", ""),
    )


async def list_threads(
    db: AsyncIOMotorDatabase, channel: dict[str, Any], reader: Any, video: dict[str, Any], limit: int
) -> list[dict[str, Any]]:
    """Threads on one video, most recently active first, each carrying any draft waiting for approval."""
    platform = str(channel.get("platform", PLATFORM_YOUTUBE))
    media_id = platform_video_id(video, platform)
    if not media_id:
        return []
    fetch = reader.get_video_threads if platform == PLATFORM_YOUTUBE else reader.get_media_threads
    raw_threads = await asyncio.to_thread(fetch, media_id, limit)
    threads = sort_threads([_shape(raw, channel, video, platform) for raw in raw_threads])

    ids = [t["thread_id"] for t in threads]
    if ids:
        drafts = await db.comment_replies.find(
            {"channel_id": channel["channel_id"], "comment_id": {"$in": ids}, "status": STATUS_PENDING}
        ).to_list(length=None)
        by_id = {d["comment_id"]: d for d in drafts}
        for thread in threads:
            draft = by_id.get(thread["thread_id"])
            if draft:
                thread["pending_draft"] = draft.get("reply_text") or draft.get("suggested_reply")
    return threads


async def load_thread(channel: dict[str, Any], reader: Any, video: dict[str, Any], thread_id: str) -> dict[str, Any]:
    """One thread, read fresh: a draft must see the replies that arrived since the list was loaded."""
    platform = str(channel.get("platform", PLATFORM_YOUTUBE))
    try:
        raw = await asyncio.to_thread(reader.get_thread, thread_id, platform_video_id(video, platform))
    except ValueError as exc:
        raise ThreadNotFoundError(thread_id) from exc
    return _shape(raw, channel, video, platform)


async def draft_reply(
    gemini: Any,
    thread: dict[str, Any],
    video: dict[str, Any],
    target_comment_id: str | None = None,
    instruction: str = "",
) -> dict[str, Any]:
    """Ask the model for the channel's next message, with the entire thread as context."""
    if target_comment_id and not find_message(thread, target_comment_id):
        raise ThreadNotFoundError(target_comment_id)
    target = find_message(thread, target_comment_id) if target_comment_id else default_target(thread)
    assert target is not None  # find_message was checked above and default_target always returns one

    text = await gemini.generate_thread_reply(
        transcript=thread_transcript(thread, target["comment_id"]),
        platform=thread["platform"],
        video_context=video_context(video),
        instruction=instruction,
    )
    return {"text": with_mention(text, target, thread) if text else "", "target_comment_id": target["comment_id"]}


async def send_reply(
    db: AsyncIOMotorDatabase,
    channel: dict[str, Any],
    poster: Any,
    thread: dict[str, Any],
    video: dict[str, Any],
    text: str,
    target_comment_id: str | None = None,
) -> dict[str, Any]:
    """Post the reply under the thread's opening comment and record it.

    Replies always go to the opening comment: neither platform nests deeper, and both file a reply
    to a reply under the same parent. When a nested message is the target an ``@mention`` is added
    so it still reads as an answer to them.
    """
    platform = thread["platform"]
    target = find_message(thread, target_comment_id) if target_comment_id else None
    final = validate_reply(with_mention(text.strip(), target, thread) if target else text, platform)

    reply_id = await asyncio.to_thread(poster.reply_to_comment, thread["thread_id"], final)

    # One row per comment_id: if the auto-reply cycle had drafted this thread, that draft is
    # superseded rather than left to be approved and posted a second time.
    await db.comment_replies.update_one(
        {"channel_id": channel["channel_id"], "comment_id": thread["thread_id"]},
        {
            "$set": {
                "video_id": video.get("video_id", ""),
                "video_title": video.get("title", ""),
                "platform": platform,
                "comment_text": thread["message"]["text"],
                "comment_author": thread["message"]["author"],
                "comment_url": thread["comment_url"],
                "status": STATUS_REPLIED,
                "reply_text": final,
                "reply_id": reply_id,
                "replied_at": now_ist(),
                "manual": True,
            },
            "$unset": {"last_error": ""},
        },
        upsert=True,
    )
    return {"reply_id": reply_id, "text": final}
