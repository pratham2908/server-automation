"""Which comment-DMs actually arrived, from Instagram's data export.

Instagram accepts a private reply to a comment and returns a message id even when the person
doesn't take message requests; the message then never reaches them. Nothing in the API or the
webhooks tells the two apart (a refused card reads back exactly like a delivered one), but the
export's DM threads note each refusal next to the private reply. ``ig_export`` reads those; this
matches them to our ``comment_dm_events`` and records ``delivery`` on each:

* ``delivered`` — the export shows the private reply with no refusal.
* ``refused`` — the export shows Instagram refusing it ("can't receive your message").

A sent DM the export can't place keeps no ``delivery`` field (unchecked), as do ones sent after
the export was made.

Matching: an export thread names the person only by display name, and by handle only in some
folder names, so a private reply is matched by the minute it went out, using the handle when the
folder has one. When several DMs went out in one minute and the export says they all went the same
way, all of them get that; a minute that mixes delivered and refused replies without handles to
tell them apart is left unchecked rather than guessed.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.ig_export import DmThread
from app.timezone import assume_utc

DELIVERED = "delivered"
REFUSED = "refused"
DELIVERY_FIELD = "delivery"
MINUTE = timedelta(minutes=1)


def squash(handle: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (handle or "").lower())


@dataclass(frozen=True)
class Notice:
    at: datetime  # UTC, floored to the minute
    refused: bool
    handle_hint: str


@dataclass(frozen=True)
class SentDm:
    id: Any
    at: datetime  # UTC
    username: str


def _minute(at: datetime) -> datetime:
    return assume_utc(at).astimezone(UTC).replace(second=0, microsecond=0)


def notices(threads: Iterable[DmThread]) -> list[Notice]:
    return [Notice(_minute(at), refused, t.handle_hint) for t in threads for at, refused in t.replies]


def match(found: Sequence[Notice], sent: Sequence[SentDm]) -> dict[Any, bool]:
    """Event id → refused, for every sent DM the export's notices place."""
    by_minute: dict[datetime, list[SentDm]] = defaultdict(list)
    for dm in sent:
        by_minute[_minute(dm.at)].append(dm)
    taken: set[Any] = set()
    out: dict[Any, bool] = {}

    def near(at: datetime) -> list[SentDm]:
        # The export's minute is Instagram's clock; ours is stamped a moment later, so the
        # same minute first, then the one either side.
        for shift in (timedelta(0), -MINUTE, MINUTE):
            free = [dm for dm in by_minute.get(at + shift, []) if dm.id not in taken]
            if free:
                return free
        return []

    rest: list[Notice] = []
    for notice in found:
        hit = next((dm for dm in near(notice.at) if squash(dm.username) == notice.handle_hint), None)
        if notice.handle_hint and hit is not None:
            taken.add(hit.id)
            out[hit.id] = notice.refused
        else:
            rest.append(notice)

    groups: dict[datetime, list[Notice]] = defaultdict(list)
    for notice in rest:
        groups[notice.at].append(notice)
    for at, group in sorted(groups.items()):
        outcomes = {n.refused for n in group}
        candidates = near(at)
        # Every candidate must be one of these notices, and the notices must agree.
        if len(outcomes) == 1 and 0 < len(candidates) <= len(group):
            for dm in candidates:
                taken.add(dm.id)
                out[dm.id] = group[0].refused
    return out


async def record_delivery(
    db: AsyncIOMotorDatabase, channel_id: str, threads: Iterable[DmThread], generated_at: datetime | None
) -> dict[str, int]:
    """Mark each sent comment-DM the export places as delivered or refused; returns the counts."""
    found = notices(threads)
    query: dict[str, Any] = {"channel_id": channel_id, "status": "sent", "sent_at": {"$ne": None}}
    if generated_at is not None:
        # Stored times are naive UTC.
        query["sent_at"] = {"$ne": None, "$lte": generated_at.astimezone(UTC).replace(tzinfo=None)}
    sent = [
        SentDm(doc["_id"], doc["sent_at"], str(doc.get("username") or ""))
        async for doc in db.comment_dm_events.find(query, {"sent_at": 1, "username": 1})
    ]
    placed = match(found, sent)
    for event_id, refused in placed.items():
        await db.comment_dm_events.update_one(
            {"_id": event_id},
            {"$set": {DELIVERY_FIELD: REFUSED if refused else DELIVERED, "delivery_source": "export"}},
        )
    refused_count = sum(1 for r in placed.values() if r)
    return {
        "private_replies_in_export": len(found),
        "refused_in_export": sum(1 for n in found if n.refused),
        "dms_checked": len(placed),
        "dms_delivered": len(placed) - refused_count,
        "dms_refused": refused_count,
        "dms_unchecked": len(sent) - len(placed),
    }
