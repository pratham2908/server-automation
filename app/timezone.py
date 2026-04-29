"""Centralised timezone helper — all timestamps use IST (GMT+5:30)."""

from datetime import datetime, timedelta, timezone
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def now_ist() -> datetime:
    """Return the current datetime in IST (GMT+5:30), timezone-aware."""
    return datetime.now(IST)


def to_ist_iso(dt: Optional[datetime | str]) -> Optional[str]:
    """Convert a datetime (or ISO string) to IST (GMT+5:30) and return ISO format string.

    Naive datetimes are assumed UTC (e.g. from MongoDB). Aware datetimes are converted to IST.
    If a string is provided, it is parsed first. Returns None if dt is None.
    """
    if dt is None:
        return None
    
    if isinstance(dt, str):
        from dateutil.parser import parse
        try:
            dt = parse(dt)
        except (ValueError, TypeError):
            return dt  # Return as-is if unparseable
            
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    ist_dt = dt.astimezone(IST)
    return ist_dt.isoformat()
