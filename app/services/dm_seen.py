"""Read receipts on our Instagram DMs (the ``messaging_seen`` webhook): when someone opens a thread.

Why (owner, 11-13): Instagram accepts a comment-DM to someone who blocks message requests, returns a
message id, and never says it wasn't delivered; only a data export shows the refusal, days later. A
receipt is live proof a DM arrived, so ``dm_seen`` keeps each one and ``signal_report`` counts how many
comment-DMs show any sign of arriving (opened, or a tap or reply). A missing receipt proves nothing:
people can switch receipts off, and Instagram sends none for messages read before we subscribed.

Shape: ``{"object": "instagram", "entry": [{"id": <account>, "messaging": [{"sender": {"id": <them>},
"recipient": {"id": <us>}, "timestamp": <ms>, "read": {"mid": <newest message they've read>}}]}]}``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.logger import get_logger
from app.services.comment_dm_store import channel_for_webhook, own_ids
from app.timezone import assume_utc

logger = get_logger(__name__)

WEBHOOK_FIELD = "messaging_seen"


@dataclass(frozen=True)
class SeenEvent:
    account_id: str
    peer_id: str
    message_id: str
    seen_at: datetime


def parse_seen_webhook(payload: Mapping[str, Any]) -> list[SeenEvent]:
    """The read receipts in one webhook delivery; messages, taps and anything else are left out."""
    if payload.get("object") != "instagram":
        return []
    events: list[SeenEvent] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, Mapping):
            continue
        for item in entry.get("messaging") or []:
            receipt = item.get("read") if isinstance(item, Mapping) else None
            if not isinstance(receipt, Mapping) or not receipt.get("mid"):
                continue
            stamp = item.get("timestamp") or entry.get("time")
            if not isinstance(stamp, int | float) or stamp <= 0:
                continue
            events.append(
                SeenEvent(
                    account_id=str(entry.get("id") or ""),
                    peer_id=str((item.get("sender") or {}).get("id") or ""),
                    message_id=str(receipt["mid"]),
                    seen_at=datetime.fromtimestamp(stamp / 1000 if stamp > 1e11 else stamp, tz=UTC),
                )
            )
    return events


async def record_webhook(db: AsyncIOMotorDatabase, payload: Mapping[str, Any], now: datetime) -> int:
    """Keep each new receipt (the first time a message was seen); never raises, so comments aren't affected."""
    kept = 0
    for event in parse_seen_webhook(payload):
        try:
            channel = await channel_for_webhook(db, event.account_id, "")
            if channel is None or not event.peer_id or event.peer_id in own_ids(channel):
                continue  # an account we don't run, or our own side reading their message
            key = {"channel_id": channel["channel_id"], "peer_id": event.peer_id, "message_id": event.message_id}
            if await db.dm_seen.find_one(key, {"_id": 1}):
                continue
            await db.dm_seen.insert_one({**key, "seen_at": event.seen_at, "logged_at": now})
            kept += 1
        except DuplicateKeyError:
            continue  # the same receipt delivered twice at once
        except Exception as exc:  # noqa: BLE001 - a lost receipt only thins the test's numbers
            logger.warning("[DmSeen] Keeping receipt for %s failed: %s", event.message_id, exc)
    return kept


def signal_report(
    sent: Sequence[tuple[str, datetime]],
    seen: Mapping[str, Sequence[datetime]],
    acted: Mapping[str, Sequence[datetime]],
) -> dict[str, int]:
    """How many sent DMs (``(peer id, sent at)``) show a sign of arriving after they went out.

    ``seen`` and ``acted`` map a person to when they opened the thread, and when they tapped or wrote.
    """

    def after(times: Sequence[datetime] | None, at: datetime) -> bool:
        return any(assume_utc(t) >= assume_utc(at) for t in times or ())

    out = {"sent": len(sent), "seen": 0, "acted": 0, "any_sign": 0, "no_sign": 0}
    for peer, at in sent:
        was_seen, did_act = after(seen.get(peer), at), after(acted.get(peer), at)
        out["seen"] += was_seen
        out["acted"] += did_act
        out["any_sign" if was_seen or did_act else "no_sign"] += 1
    return out
