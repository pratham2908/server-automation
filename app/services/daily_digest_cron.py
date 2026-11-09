"""Once a day, email how every active channel did the day before.

Sent from 08:00 IST, after the 00:00 daily sync and the quarter-hour syncs that carried the
day to midnight, so yesterday's numbers are final and the mail is at the top of the inbox in
the morning. A latch document per date makes sure it goes out once, and a failed send is
retried on the next tick, a few times, before the day is given up on.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from app.config import Settings, get_settings
from app.logger import get_logger
from app.services.daily_digest import build_digest
from app.services.daily_digest_email import format_digest_email
from app.services.email_service import resolve_owner_email, send_email
from app.services.error_reporting import report_error
from app.timezone import now_ist

logger = get_logger(__name__)

SEND_HOUR_IST = 8
TICK_SECONDS = 300
MAX_ATTEMPTS = 3


async def send_digest(db: Any, settings: Settings, day: date) -> str:
    """Build and send one day's digest. Returns "sent", "no active channels" or "not sent"."""
    digest = await build_digest(db, day)
    if not digest["channels"]:
        return "no active channels"
    email = format_digest_email(digest, settings.ANALYZER_PUBLIC_URL)
    recipient = await resolve_owner_email(db, settings)
    return "sent" if await send_email(settings, recipient, email.subject, email.text, email.html) else "not sent"


async def run_due_digest(db: Any, settings: Settings) -> str | None:
    """One tick: send yesterday's digest if it is time and it has not gone out. Returns the outcome, if any."""
    now = now_ist()
    if now.hour < SEND_HOUR_IST:
        return None
    day = now.date() - timedelta(days=1)
    key = day.isoformat()
    await db.daily_digests.update_one(
        {"date": key}, {"$setOnInsert": {"date": key, "done": False, "attempts": 0}}, upsert=True
    )
    # Claim an attempt; only the tick that wins it sends, and a finished day matches nothing.
    claimed = await db.daily_digests.find_one_and_update(
        {"date": key, "done": False, "attempts": {"$lt": MAX_ATTEMPTS}}, {"$inc": {"attempts": 1}}
    )
    if claimed is None:
        return None
    outcome = await send_digest(db, settings, day)
    if outcome != "not sent":
        await db.daily_digests.update_one({"date": key}, {"$set": {"done": True, "outcome": outcome, "at": now}})
    else:
        logger.warning("Daily digest for %s not sent (attempt %d of %d)", key, claimed["attempts"] + 1, MAX_ATTEMPTS)
    return outcome


async def run_daily_digest_cron(db: Any) -> None:
    logger.info("Daily digest cron started (sends from %02d:00 IST)", SEND_HOUR_IST)
    while True:
        try:
            await run_due_digest(db, get_settings())
        except Exception as exc:
            logger.error("Daily digest failed: %s", exc)
            await report_error(feature="Daily digest cron", message=f"Daily digest failed: {exc!s}", exception=exc)
        await asyncio.sleep(TICK_SECONDS)
