"""Centralised timezone helper — all timestamps use IST (GMT+5:30)."""

from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """Return the current datetime in IST (GMT+5:30), timezone-aware."""
    return datetime.now(IST)
