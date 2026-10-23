"""When a Threads token is refreshed, and when it is a lost cause.

Pure: no I/O.

A long-lived Threads token lasts 60 days and can be refreshed for another 60 —
but only once it is at least a day old and before it expires. One that lapses
can't be revived; the account has to log in again. So refreshing weekly keeps a
wide margin, and a token close to expiry that still won't refresh is worth a
warning while there is time to act.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from app.timezone import assume_utc

TOKEN_LIFETIME = timedelta(days=60)
REFRESH_EVERY = timedelta(days=7)
MIN_AGE_TO_REFRESH = timedelta(days=1)
WARN_WITHIN = timedelta(days=10)

TokenState = Literal["missing", "fresh", "refresh", "expired"]


def _at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return assume_utc(value)
    if isinstance(value, str) and value:
        try:
            return assume_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def expires_at(tokens: dict[str, Any] | None) -> datetime | None:
    """When the token dies: the stored expiry, else 60 days from when it was issued or refreshed."""
    if not tokens:
        return None
    stored = _at(tokens.get("expires_at"))
    if stored:
        return stored
    issued = _at(tokens.get("refreshed_at")) or _at(tokens.get("issued_at"))
    return issued + TOKEN_LIFETIME if issued else None


def token_state(tokens: dict[str, Any] | None, now: datetime) -> TokenState:
    """What the refresher should do with this token now."""
    if not tokens or not tokens.get("access_token"):
        return "missing"
    expiry = expires_at(tokens)
    if expiry is not None and expiry <= now:
        return "expired"
    issued = _at(tokens.get("refreshed_at")) or _at(tokens.get("issued_at"))
    if issued is None:
        # Age unknown: try a refresh; Threads refuses one that is too young, which costs one call.
        return "refresh"
    age = now - issued
    if age < MIN_AGE_TO_REFRESH:
        return "fresh"
    return "refresh" if age >= REFRESH_EVERY else "fresh"


def should_warn(tokens: dict[str, Any] | None, now: datetime) -> bool:
    """Whether the token dies within the warning window — said once a refresh has failed."""
    expiry = expires_at(tokens)
    return expiry is not None and now < expiry <= now + WARN_WITHIN
