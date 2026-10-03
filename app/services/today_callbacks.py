"""One-time callbacks a source app uses to hand us "today's video" when it is done.

Asking ``/today`` every few minutes until a render lands has two costs: the slot
learns about a finished video up to a tick late, and every ask is another nudge to
the app (GeoRank re-dispatched a failed render on each one). So each ask now also
offers a callback — a URL naming this record and a password minted for it — and an
app that accepts it calls back once instead of being polled.

Only a hash of the password is stored. The plaintext leaves exactly once, in a
request header to the app we are asking, never in a URL: URLs land in access logs
on both sides, headers do not.

A record is ``pending`` until the app calls (``received``), the slot stops needing
it (``closed``), or the day it belongs to is over (``expired``). Anything but
``pending`` refuses a call, which is what makes the password single-use.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from app.logger import get_logger
from app.timezone import IST, assume_utc

logger = get_logger(__name__)

# Request headers the offer travels in. The id is also the URL's last segment; it
# travels on its own so the app can key and log the callback without parsing a URL.
CALLBACK_ID_HEADER = "X-Callback-Id"
CALLBACK_URL_HEADER = "X-Callback-Url"
CALLBACK_TOKEN_HEADER = "X-Callback-Token"
CALLBACK_PATH = "/api/v1/source-callbacks"

CallbackStatus = Literal["pending", "received", "closed", "expired"]


@dataclass(frozen=True, slots=True)
class CallbackOffer:
    """What we hand an app alongside a ``/today`` ask. ``token`` is never stored."""

    callback_id: str
    url: str
    token: str

    def headers(self) -> dict[str, str]:
        return {
            CALLBACK_ID_HEADER: self.callback_id,
            CALLBACK_URL_HEADER: self.url,
            CALLBACK_TOKEN_HEADER: self.token,
        }


@dataclass(frozen=True, slots=True)
class CallbackSlot:
    """Which slot an offer is for, and where this server can be reached."""

    channel_id: str
    day: date
    slot: str
    public_base_url: str


# ------------------------------------------------------------------
# Pure helpers
# ------------------------------------------------------------------


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_matches(token: str, token_hash: str) -> bool:
    # Constant time, so a wrong guess cannot learn how much of the hash it matched.
    return hmac.compare_digest(hash_token(token), token_hash)


def callback_url(public_base_url: str, callback_id: str) -> str:
    return f"{public_base_url.rstrip('/')}{CALLBACK_PATH}/{callback_id}"


def callback_expiry(day: date) -> datetime:
    """Midnight IST after ``day``: a slot's video is no use once its day is over."""
    return datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=IST)


def bearer_token(authorization: str | None) -> str | None:
    """The token from an ``Authorization: Bearer …`` header, or ``None``."""
    if not authorization:
        return None
    scheme, _, value = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


# ------------------------------------------------------------------
# Storage
# ------------------------------------------------------------------


async def issue(db: Any, slot: CallbackSlot, source_id: str, now: datetime) -> CallbackOffer:
    callback_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    await db.source_callbacks.insert_one(
        {
            "callback_id": callback_id,
            "token_hash": hash_token(token),
            "channel_id": slot.channel_id,
            "source_id": source_id,
            "day": slot.day.isoformat(),
            "slot": slot.slot,
            "status": "pending",
            "created_at": now,
            "expires_at": callback_expiry(slot.day),
        }
    )
    return CallbackOffer(callback_id=callback_id, url=callback_url(slot.public_base_url, callback_id), token=token)


async def close(db: Any, callback_id: str, now: datetime, reason: str) -> None:
    """The slot no longer needs this callback — refuse it from now on."""
    await db.source_callbacks.update_one(
        {"callback_id": callback_id, "status": "pending"},
        {"$set": {"status": "closed", "closed_at": now, "closed_reason": reason}},
    )


async def expire_overdue(db: Any, now: datetime) -> int:
    """Expire pending callbacks whose day is over — the fallback for an app that never called."""
    result = await db.source_callbacks.update_many(
        {"status": "pending", "expires_at": {"$lte": now}},
        {"$set": {"status": "expired", "closed_at": now}},
    )
    if result.modified_count:
        logger.info("Expired %d source callback(s) that were never called", result.modified_count)
    return int(result.modified_count)


ClaimOutcome = Literal["claimed", "not_found", "unauthorized", "already_received", "gone"]


async def claim(db: Any, callback_id: str, token: str | None, now: datetime) -> tuple[ClaimOutcome, dict[str, Any]]:
    """Take a callback for processing, exactly once.

    The token is checked before the status, so an unauthenticated caller learns
    nothing about a record beyond the fact that its id exists.
    """
    record = await db.source_callbacks.find_one({"callback_id": callback_id})
    if not record:
        return "not_found", {}
    if not token or not token_matches(token, str(record.get("token_hash") or "")):
        return "unauthorized", {}
    status = record.get("status")
    if status == "received":
        return "already_received", record
    if status != "pending":
        return "gone", record
    if assume_utc(record["expires_at"]) <= now:
        await db.source_callbacks.update_one(
            {"callback_id": callback_id, "status": "pending"},
            {"$set": {"status": "expired", "closed_at": now}},
        )
        return "gone", record

    # The atomic flip is what makes the password single-use: two deliveries racing
    # each other both pass the checks above, only one matches here.
    won = await db.source_callbacks.find_one_and_update(
        {"callback_id": callback_id, "status": "pending"},
        {"$set": {"status": "received", "received_at": now}},
    )
    if won is None:
        return "already_received", record
    return "claimed", record


async def record_outcome(db: Any, callback_id: str, outcome: str) -> None:
    await db.source_callbacks.update_one({"callback_id": callback_id}, {"$set": {"outcome": outcome}})
