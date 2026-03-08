"""Centralised timezone helper — all timestamps use IST (GMT+5:30)."""

from datetime import datetime, timezone, timedelta
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def now_ist() -> datetime:
    """Return the current datetime in IST (GMT+5:30), timezone-aware."""
    return datetime.now(IST)


def to_ist_iso(dt: Optional[datetime]) -> Optional[str]:
    """Convert a datetime to IST (GMT+5:30) and return ISO format string for API responses.

    Naive datetimes are assumed UTC (e.g. from MongoDB). Aware datetimes are converted to IST.
    Returns None if dt is None. Use this so all timestamps in responses show +05:30.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    ist_dt = dt.astimezone(IST)
    return ist_dt.isoformat()
