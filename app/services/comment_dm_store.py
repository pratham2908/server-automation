"""Where comment-to-DM keeps its rules and its queue of comments to answer.

- A channel's default rule sits on the channel document (``comment_dm_default``); it covers every reel
  that has no rule of its own, so a newly posted reel is covered the moment it exists.
- A reel's own rule lives in ``comment_dm_rules``, keyed by channel and Instagram media id, so it
  works for any reel on the account, including ones posted outside this app.
- A reel that is still scheduled has no media id yet, so its rule lives in ``comment_dm_video_rules``,
  keyed by our video id. Once the reel publishes, its video document carries the media id, and the
  rule is found through it — nothing has to be moved at publish time.
- Every comment that earns a DM becomes one ``comment_dm_events`` document, keyed by comment id.
  That key is what keeps a comment from being messaged twice when Meta's webhook and our own poll
  both report it, or the webhook arrives twice.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.services.comment_dm import (
    LOGGED_SKIPS,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_SKIPPED,
    CommentEvent,
    DmRule,
    StoredRule,
    effective_rule,
    matched_trigger,
    person_key,
    pick_reply,
    send_delay,
    skip_reason,
    stored_rule,
)
from app.services.instagram_tokens import ALT_FIELD, PRIMARY_FIELD

DEFAULT_FIELD = "comment_dm_default"
# Set when the account is subscribed to webhooks: the id Meta addresses them to, which for an
# Instagram Login channel is not the id the channel is stored under.
WEBHOOK_ID_FIELD = "instagram_webhook_id"

QUEUED = "queued"
DUPLICATE = "duplicate"
NO_RULE = "no rule"
NO_MATCH = "no match"


def _stored(doc: dict[str, Any] | None) -> StoredRule | None:
    return StoredRule.model_validate(doc) if doc else None


async def load_default(db: AsyncIOMotorDatabase, channel_id: str) -> StoredRule | None:
    channel = await db.channels.find_one({"channel_id": channel_id}, {DEFAULT_FIELD: 1})
    return _stored((channel or {}).get(DEFAULT_FIELD))


async def load_reel_rule(db: AsyncIOMotorDatabase, channel_id: str, media_id: str) -> StoredRule | None:
    doc = await db.comment_dm_rules.find_one({"channel_id": channel_id, "media_id": media_id})
    return _stored(doc)


async def load_video_rule(db: AsyncIOMotorDatabase, channel_id: str, video_id: str) -> StoredRule | None:
    doc = await db.comment_dm_video_rules.find_one({"channel_id": channel_id, "video_id": video_id})
    return _stored(doc)


async def list_video_rules(db: AsyncIOMotorDatabase, channel_id: str) -> dict[str, StoredRule]:
    rules: dict[str, StoredRule] = {}
    async for doc in db.comment_dm_video_rules.find({"channel_id": channel_id}):
        rules[str(doc["video_id"])] = StoredRule.model_validate(doc)
    return rules


async def save_video_rule(
    db: AsyncIOMotorDatabase, channel_id: str, video_id: str, rule: DmRule, now: datetime
) -> StoredRule:
    saved = stored_rule(rule, await load_video_rule(db, channel_id, video_id), now)
    await db.comment_dm_video_rules.update_one(
        {"channel_id": channel_id, "video_id": video_id}, {"$set": saved.model_dump()}, upsert=True
    )
    return saved


async def clear_video_rule(db: AsyncIOMotorDatabase, channel_id: str, video_id: str) -> None:
    await db.comment_dm_video_rules.delete_one({"channel_id": channel_id, "video_id": video_id})


async def own_rules_by_media(db: AsyncIOMotorDatabase, channel_id: str) -> dict[str, StoredRule]:
    """Every reel-specific rule keyed by media id: rules set on posted reels, plus rules set while a reel was
    scheduled whose video has since published. A media-keyed rule wins when both exist."""
    rules: dict[str, StoredRule] = {}
    video_rules = await list_video_rules(db, channel_id)
    if video_rules:
        cursor = db.videos.find(
            {"channel_id": channel_id, "video_id": {"$in": list(video_rules)}, "instagram_media_id": {"$exists": True}},
            {"video_id": 1, "instagram_media_id": 1},
        )
        async for video in cursor:
            if video.get("instagram_media_id"):
                rules[str(video["instagram_media_id"])] = video_rules[str(video["video_id"])]
    rules.update(await list_reel_rules(db, channel_id))
    return rules


async def list_reel_rules(db: AsyncIOMotorDatabase, channel_id: str) -> dict[str, StoredRule]:
    rules: dict[str, StoredRule] = {}
    async for doc in db.comment_dm_rules.find({"channel_id": channel_id}):
        rules[str(doc["media_id"])] = StoredRule.model_validate(doc)
    return rules


async def save_default(db: AsyncIOMotorDatabase, channel_id: str, rule: DmRule, now: datetime) -> StoredRule:
    saved = stored_rule(rule, await load_default(db, channel_id), now)
    await db.channels.update_one({"channel_id": channel_id}, {"$set": {DEFAULT_FIELD: saved.model_dump()}})
    return saved


async def clear_default(db: AsyncIOMotorDatabase, channel_id: str) -> None:
    await db.channels.update_one({"channel_id": channel_id}, {"$unset": {DEFAULT_FIELD: ""}})


async def save_reel_rule(
    db: AsyncIOMotorDatabase, channel_id: str, media_id: str, rule: DmRule, now: datetime
) -> StoredRule:
    saved = stored_rule(rule, await load_reel_rule(db, channel_id, media_id), now)
    await db.comment_dm_rules.update_one(
        {"channel_id": channel_id, "media_id": media_id}, {"$set": saved.model_dump()}, upsert=True
    )
    return saved


async def clear_reel_rule(db: AsyncIOMotorDatabase, channel_id: str, media_id: str) -> None:
    await db.comment_dm_rules.delete_one({"channel_id": channel_id, "media_id": media_id})


async def _own_rule(db: AsyncIOMotorDatabase, channel_id: str, media_id: str) -> StoredRule | None:
    """The rule set for this reel itself: on the posted reel, or on it while it was still scheduled."""
    reel = await load_reel_rule(db, channel_id, media_id)
    if reel is not None:
        return reel
    video = await db.videos.find_one({"channel_id": channel_id, "instagram_media_id": media_id}, {"video_id": 1})
    return await load_video_rule(db, channel_id, str(video["video_id"])) if video else None


async def active_rule(db: AsyncIOMotorDatabase, channel_id: str, media_id: str) -> StoredRule | None:
    return effective_rule(await _own_rule(db, channel_id, media_id), await load_default(db, channel_id))


def own_ids(channel: dict[str, Any]) -> set[str]:
    """Every id the channel's own comments can arrive under."""
    ids = {str(channel.get("instagram_user_id") or ""), str(channel.get(WEBHOOK_ID_FIELD) or "")}
    for field in (PRIMARY_FIELD, ALT_FIELD):
        ids.add(str((channel.get(field) or {}).get("instagram_user_id") or ""))
    ids.discard("")
    return ids


async def channel_for_webhook(db: AsyncIOMotorDatabase, account_id: str, media_id: str) -> dict[str, Any] | None:
    """The channel a webhook comment belongs to: by the account it was addressed to, else by its reel."""
    if account_id:
        for field in (
            WEBHOOK_ID_FIELD,
            "instagram_user_id",
            f"{PRIMARY_FIELD}.instagram_user_id",
            f"{ALT_FIELD}.instagram_user_id",
        ):
            channel = await db.channels.find_one({"platform": "instagram", field: account_id})
            if channel:
                return dict(channel)
    video = await db.videos.find_one({"instagram_media_id": media_id}, {"channel_id": 1})
    channel = await db.channels.find_one({"channel_id": video["channel_id"]}) if video else None
    return dict(channel) if channel else None


async def _already_messaged(db: AsyncIOMotorDatabase, channel_id: str, media_id: str, person: str) -> bool:
    found = await db.comment_dm_events.find_one(
        {
            "channel_id": channel_id,
            "media_id": media_id,
            "person": person,
            "status": {"$in": [STATUS_PENDING, STATUS_SENDING, STATUS_SENT]},
        },
        {"_id": 1},
    )
    return found is not None


async def enqueue(
    db: AsyncIOMotorDatabase,
    channel: dict[str, Any],
    event: CommentEvent,
    *,
    source: str,
    now: datetime,
    rng: random.Random,
) -> str:
    """Queue a DM for this comment if its reel's rule wants one. Returns what happened, for logs and tests."""
    channel_id = str(channel["channel_id"])
    if await db.comment_dm_events.find_one({"comment_id": event.comment_id}, {"_id": 1}):
        return DUPLICATE
    active = await active_rule(db, channel_id, event.media_id)
    if active is None:
        return NO_RULE
    trigger = matched_trigger(active.rule, event.text)
    if trigger is None:
        return NO_MATCH
    person = person_key(event)
    reason = skip_reason(
        event,
        own_ids=own_ids(channel),
        own_username=str(channel.get("instagram_username") or ""),
        active_since=active.enabled_at,
        now=now,
        already_messaged=await _already_messaged(db, channel_id, event.media_id, person),
    )
    if reason is not None and reason not in LOGGED_SKIPS:
        return reason
    doc = {
        "comment_id": event.comment_id,
        "channel_id": channel_id,
        "media_id": event.media_id,
        "person": person,
        "username": event.username,
        "from_id": event.from_id,
        "text": event.text,
        "parent_id": event.parent_id,
        "commented_at": event.created_at,
        "trigger": trigger,
        "source": source,
        "dm_text": active.rule.dm_text,
        "reply_text": pick_reply(active.rule, rng),
        "buttons": [b.model_dump() for b in active.rule.buttons],
        "status": STATUS_SKIPPED if reason else STATUS_PENDING,
        "skip_reason": reason,
        "send_after": now + send_delay(active.rule, rng),
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
    }
    try:
        await db.comment_dm_events.insert_one(doc)
    except DuplicateKeyError:
        # Webhook and poll raced on the same comment; the unique index let exactly one through.
        return DUPLICATE
    return reason or QUEUED


async def claim_due(db: AsyncIOMotorDatabase, now: datetime) -> dict[str, Any] | None:
    """Take the oldest DM that is due, marking it in flight so no other pass sends it too."""
    doc = await db.comment_dm_events.find_one_and_update(
        {"status": STATUS_PENDING, "send_after": {"$lte": now}},
        {"$set": {"status": STATUS_SENDING, "claimed_at": now, "updated_at": now}},
        sort=[("send_after", 1)],
        return_document=ReturnDocument.AFTER,
    )
    return dict(doc) if doc else None


async def sent_since(db: AsyncIOMotorDatabase, channel_id: str, since: datetime) -> int:
    return int(
        await db.comment_dm_events.count_documents(
            {"channel_id": channel_id, "status": STATUS_SENT, "sent_at": {"$gte": since}}
        )
    )


async def update_event(db: AsyncIOMotorDatabase, comment_id: str, fields: dict[str, Any], now: datetime) -> None:
    await db.comment_dm_events.update_one({"comment_id": comment_id}, {"$set": {**fields, "updated_at": now}})


async def fail_interrupted(db: AsyncIOMotorDatabase, stale_before: datetime, now: datetime) -> int:
    """Sends cut off mid-flight (a restart) are failed, not retried: the DM may already have gone out,
    and a second one to the same person is worse than a missing one."""
    result = await db.comment_dm_events.update_many(
        {"status": STATUS_SENDING, "claimed_at": {"$lte": stale_before}},
        {"$set": {"status": STATUS_FAILED, "error": "interrupted while sending; not retried", "updated_at": now}},
    )
    return int(result.modified_count)


async def list_events(
    db: AsyncIOMotorDatabase, channel_id: str, media_id: str | None, limit: int
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"channel_id": channel_id}
    if media_id:
        query["media_id"] = media_id
    return list(await db.comment_dm_events.find(query, {"_id": 0}).sort("created_at", -1).limit(limit).to_list(limit))


async def summary(db: AsyncIOMotorDatabase, channel_id: str, since: datetime | None) -> dict[str, int]:
    """How many comments earned a DM since *since* (all time when None), and how each one went."""
    base: dict[str, Any] = {"channel_id": channel_id}
    if since is not None:
        base["created_at"] = {"$gte": since}

    async def count(extra: dict[str, Any]) -> int:
        return int(await db.comment_dm_events.count_documents({**base, **extra}))

    return {
        "comments": await count({}),
        "dms_sent": await count({"status": STATUS_SENT}),
        "replies_posted": await count({"reply_comment_id": {"$exists": True}}),
        "queued": await count({"status": {"$in": [STATUS_PENDING, STATUS_SENDING]}}),
        "failed": await count({"status": STATUS_FAILED}),
        "skipped": await count({"status": STATUS_SKIPPED}),
    }


async def channels_with_rules(db: AsyncIOMotorDatabase) -> list[str]:
    """Channels with any rule switched on — the only ones worth polling."""
    ids = {str(c["channel_id"]) async for c in db.channels.find({f"{DEFAULT_FIELD}.rule.enabled": True})}
    ids |= {str(r["channel_id"]) async for r in db.comment_dm_rules.find({"rule.enabled": True})}
    ids |= {str(r["channel_id"]) async for r in db.comment_dm_video_rules.find({"rule.enabled": True})}
    return sorted(ids)
