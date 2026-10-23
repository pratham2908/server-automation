"""Keeps every channel's Threads token alive, and says so when one can't be.

A Threads token lasts 60 days and can only be refreshed while it is still alive,
so a token nobody refreshes dies quietly — and takes the channel's publishing
with it. This loop refreshes each one weekly (see ``threads_token_policy``), and
emails the owner when a refresh fails close to expiry or a token has already
died, at most once a day per channel, so there is time to log in again.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import requests

from app.config import get_settings
from app.logger import get_logger
from app.services.email_service import resolve_owner_email, send_email
from app.services.threads import refresh_long_lived, token_doc
from app.services.threads_token_policy import expires_at, should_warn, token_state
from app.timezone import assume_utc, now_ist

logger = get_logger(__name__)

_TICK_SECONDS = 6 * 3600
_NOTIFY_EVERY = timedelta(hours=23)


async def _notify(db: Any, channel: dict[str, Any], what: str, now: datetime) -> None:
    """Email the owner about this channel's token, once a day at most."""
    tokens = channel.get("threads_tokens") or {}
    last = tokens.get("notified_at")
    if isinstance(last, datetime) and now - assume_utc(last) < _NOTIFY_EVERY:
        return
    settings = get_settings()
    name = channel.get("name") or channel["channel_id"]
    subject = f"Threads: reconnect {name}"
    body = (
        f"{what}\n\nUntil it is reconnected, posts scheduled to {name} on Threads will fail and its "
        f"numbers stop updating. Reconnect it under Settings › Threads in the dashboard."
    )
    if settings.ANALYZER_PUBLIC_URL:
        body += f"\n\n{settings.ANALYZER_PUBLIC_URL}"
    await send_email(settings, await resolve_owner_email(db, settings), subject, body)
    await db.channels.update_one({"channel_id": channel["channel_id"]}, {"$set": {"threads_tokens.notified_at": now}})


async def refresh_channel_token(db: Any, channel: dict[str, Any], now: datetime) -> str:
    """Refresh or warn for one channel. Returns what happened, for the log."""
    tokens = channel.get("threads_tokens")
    state = token_state(tokens, now)
    if state in ("missing", "fresh"):
        return state
    if state == "expired":
        await _notify(db, channel, "Its Threads login has expired and can't be refreshed any more.", now)
        return "expired"
    assert tokens is not None  # token_state returned "refresh", so a token exists
    try:
        body = await asyncio.to_thread(refresh_long_lived, tokens["access_token"])
    except requests.HTTPError as exc:
        logger.warning("Threads token refresh failed for '%s': %s", channel["channel_id"], exc)
        if should_warn(tokens, now):
            expiry = expires_at(tokens)
            when = f" on {expiry:%d %b}" if expiry else ""
            await _notify(db, channel, f"Its Threads login couldn't be refreshed and expires{when}.", now)
        return "failed"
    fresh = token_doc(body, tokens.get("user_id"))
    await db.channels.update_one({"channel_id": channel["channel_id"]}, {"$set": {"threads_tokens": fresh}})
    logger.info("Refreshed Threads token for '%s'", channel["channel_id"])
    return "refreshed"


async def run_threads_token_refresher(db: Any) -> None:
    """Long-running loop: every few hours, refresh or warn for each connected Threads channel."""
    logger.info("[Threads] Token refresher started (tick: %ds)", _TICK_SECONDS)
    while True:
        try:
            now = now_ist()
            async for channel in db.channels.find({"threads_tokens.access_token": {"$exists": True}}):
                await refresh_channel_token(db, channel, now)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[Threads] Token refresher tick failed")
        await asyncio.sleep(_TICK_SECONDS)
