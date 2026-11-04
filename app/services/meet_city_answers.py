"""Answer a city typed in reply to the meet flow's "Which city is your group in?", and keep a tally.

A typed reply is answered only when the LAST message we sent them is that question, sent at most
``ANSWER_WINDOW`` before it: anything of ours since (the chip's reply, a menu reply) means they're
talking about something else. Only the first typed reply to one question is answered.

Every answer is kept in ``dm_city_answers`` (one row per message, so a redelivered webhook does nothing
twice) — that collection is the city-demand tally. The reply goes through the tap outbox
(``dm_outbox``), so the comment-DM worker sends it moments later, the way it sends a chip's reply.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import ValidationError

from app.logger import get_logger
from app.services import dm_outbox, meet_city
from app.services.comment_dm_store import channel_for_webhook, own_ids
from app.services.dm_content import DmMessage
from app.services.dm_store import DmEvent, parse_message_webhook
from app.timezone import assume_utc

logger = get_logger(__name__)

ANSWER_WINDOW = timedelta(minutes=30)
REASON = "typed_city"

NOT_THIS_FLOW = "not this flow"
NOT_AFTER_PROMPT = "not a reply to the city question"
ALREADY_ANSWERED = "question already answered"
DUPLICATE = "duplicate"
IGNORED = "not a city"
QUEUED = "queued"


async def chip_reply(db: AsyncIOMotorDatabase, channel_id: str, slug: str) -> DmMessage | None:
    """What this city's chip sends now: the newest stored reply whose map link is that chip's."""
    marker = re.compile(re.escape(meet_city.chip_content(slug)) + r"(&|$)")
    rows = await db.dm_replies.find({"channel_id": channel_id}).to_list(length=None)
    hits = [
        r
        for r in rows
        if any(marker.search(str(b.get("url") or "")) for b in (r.get("message") or {}).get("buttons") or [])
    ]
    for row in sorted(hits, key=lambda r: assume_utc(r["created_at"]), reverse=True):
        try:
            return DmMessage.model_validate(row["message"])
        except ValidationError:
            continue
    return None


async def _last_from_us(db: AsyncIOMotorDatabase, channel_id: str, peer_id: str, before: datetime) -> dict[str, Any]:
    rows = (
        await db.dm_messages.find(
            {"channel_id": channel_id, "peer_id": peer_id, "from_me": True, "created_at": {"$lte": before}}
        )
        .sort("created_at", -1)
        .limit(1)
        .to_list(length=1)
    )
    return rows[0] if rows else {}


async def handle(db: AsyncIOMotorDatabase, channel: Mapping[str, Any], event: DmEvent, now: datetime) -> str:
    """Answer one webhook message if it's a city typed for the meet flow. Returns what happened."""
    channel_id = str(channel["channel_id"])
    if channel_id != meet_city.CHANNEL_ID:
        return NOT_THIS_FLOW
    sender = event.sender_id
    if (
        event.is_echo
        or event.is_chip
        or event.is_deleted
        or event.kind != "text"
        or not event.text.strip()
        or not sender
        or sender == event.account_id
        or sender in own_ids(dict(channel))
    ):
        return NOT_THIS_FLOW
    said_at = assume_utc(event.created_at)
    prompt = await _last_from_us(db, channel_id, sender, said_at)
    asked_at = prompt.get("created_at")
    if (
        not isinstance(asked_at, datetime)
        or not meet_city.is_prompt(str(prompt.get("text") or ""))
        or said_at - assume_utc(asked_at) > ANSWER_WINDOW
    ):
        return NOT_AFTER_PROMPT
    prompt_id = str(prompt.get("message_id") or "")
    if await db.dm_city_answers.find_one({"message_id": event.message_id}, {"_id": 1}):
        return DUPLICATE
    if await db.dm_city_answers.find_one(
        {"channel_id": channel_id, "peer_id": sender, "prompt_id": prompt_id, "replied": True}, {"_id": 1}
    ):
        return ALREADY_ANSWERED

    answer = meet_city.read_answer(event.text)
    reply: DmMessage | None = None
    sends = "none"
    if answer.outcome == "matched" and answer.slug:
        reply = await chip_reply(db, channel_id, answer.slug)
        sends = "chip" if reply is not None else "typed"
    if reply is None:
        reply = meet_city.typed_reply(answer)
        sends = "typed" if reply is not None else "none"

    result = await db.dm_city_answers.update_one(
        {"message_id": event.message_id},
        {
            "$setOnInsert": {
                "message_id": event.message_id,
                "channel_id": channel_id,
                "peer_id": sender,
                "text": event.text,
                "name": answer.name,
                "normalised": answer.normalised,
                "slug": answer.slug,
                "outcome": answer.outcome,
                "replied": reply is not None,
                "reply_kind": sends,
                "prompt_id": prompt_id,
                "prompt_at": asked_at,
                "answered_at": said_at,
                "created_at": now,
            }
        },
        upsert=True,
    )
    if result.upserted_id is None:
        return DUPLICATE
    if reply is None:
        return IGNORED
    queued = await dm_outbox.enqueue(
        db,
        key=f"meet-city:{event.message_id}",
        channel_id=channel_id,
        recipient_id=sender,
        message=reply,
        check_follow=False,
        reason=REASON,
        now=now,
    )
    return QUEUED if queued else DUPLICATE


async def record_webhook(db: AsyncIOMotorDatabase, payload: Mapping[str, Any], now: datetime) -> int:
    """Answer every typed city in a delivery; never raises, so comments, DMs and taps aren't affected."""
    queued = 0
    for event in parse_message_webhook(payload):
        try:
            channel = await channel_for_webhook(db, event.account_id, "")
            if channel is None:
                continue
            outcome = await handle(db, channel, event, now)
            if outcome not in (NOT_THIS_FLOW, NOT_AFTER_PROMPT):
                logger.info(
                    "[MeetCity] %r from %s on %s: %s", event.text[:40], event.sender_id, channel["channel_id"], outcome
                )
            queued += outcome == QUEUED
        except Exception as exc:  # noqa: BLE001 - one bad message must not lose the rest of the delivery
            logger.warning("[MeetCity] Handling message %s failed: %s", event.message_id, exc)
    return queued
