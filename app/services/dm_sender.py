"""Sending a composed DM: sign its uploaded files, register its replies, send each part, keep a copy.

The thin edge over ``dm_content``: everything here touches Instagram, R2 or Mongo, and nothing here
decides what a message looks like.
"""

from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import requests
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services import dm_replies, dm_store
from app.services.dm_content import DmMessage, SendContext, build_messages, media_keys, summary_text
from app.services.instagram import graph_error_message

logger = get_logger(__name__)

# Instagram fetches an attachment when the message is sent; an hour is ample and short enough not to linger.
MEDIA_LINK_SECONDS = 3600

Signer = Callable[[str], str]


def r2_signer() -> Signer | None:
    """Signed GET links for uploaded DM media, or None when this server has no R2."""
    import app.main as main_mod

    r2 = main_mod.r2_service
    if r2 is None:
        return None

    def sign(key: str) -> str:
        # Stored without a Content-Type, so the link names one: Instagram checks it when it fetches.
        kind = mimetypes.guess_type(key)[0]
        return str(r2.generate_presigned_url(key, expires_in=MEDIA_LINK_SECONDS, response_content_type=kind))

    return sign


def context_for(channel: Mapping[str, Any], message: DmMessage, signer: Signer | None) -> SendContext:
    """What building *message* needs from this channel: its handle and links for its uploaded files."""
    keys = media_keys(message)
    if keys and signer is None:
        raise ValueError("this message uses uploaded files, but the server has no file storage configured")
    return SendContext(
        channel_id=str(channel["channel_id"]),
        username=str(channel.get("instagram_username") or ""),
        media_urls={k: signer(k) for k in keys} if signer else {},
    )


async def follows_us(service: Any, recipient_id: str) -> bool | None:
    """Whether they follow us, or None when Instagram won't say (it then fails open: see the caller)."""
    try:
        profile = await asyncio.to_thread(service.messaging_profile, recipient_id)
    except requests.RequestException as exc:
        logger.warning("[DmSender] Follow check for %s failed: %s", recipient_id, graph_error_message(exc))
        return None
    value = profile.get("is_user_follow_business")
    return value if isinstance(value, bool) else None


async def deliver(
    db: AsyncIOMotorDatabase,
    service: Any,
    ig_user_id: str,
    channel: Mapping[str, Any],
    recipient_id: str,
    message: DmMessage,
    now: datetime,
    signer: Signer | None = None,
) -> list[str]:
    """Send *message* to *recipient_id* as one or more Instagram messages. Returns their ids.

    Raises ``requests.RequestException`` when Instagram refuses a part, and ``ValueError`` when the
    message can't be built here (no username for a profile button, no storage for an upload). A part
    already sent stays sent: Instagram has no unsend.
    """
    channel_id = str(channel["channel_id"])
    ctx = context_for(channel, message, signer)
    parts = build_messages(message, ctx)
    # Before sending, so a tap that lands a second after the message finds its reply.
    await dm_replies.register_message(db, channel_id, message, now)
    ids: list[str] = []
    for part in parts:
        message_id = await asyncio.to_thread(service.send_message, ig_user_id, recipient_id, part)
        ids.append(message_id)
    if ids and ids[-1]:
        await dm_store.record_sent(db, channel_id, recipient_id, ids[-1], summary_text(message), now)
    return ids
