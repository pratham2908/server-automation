"""Centralised timezone helper — all timestamps use IST (GMT+5:30)."""

from datetime import datetime, timedelta, timezone

from dateutil.parser import parse

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def now_ist() -> datetime:
    """Return the current datetime in IST (GMT+5:30), timezone-aware."""
    return datetime.now(IST)


def assume_utc(dt: datetime) -> datetime:
    """Return ``dt`` timezone-aware, treating a naive value as UTC.

    MongoDB hands back naive datetimes that are UTC. Relabelling one as IST
    instead of converting it shifts the instant 5h30m into the past, which reads
    as "this happened long ago" — enough to make a wait look already exhausted on
    its first check. Every naive timestamp entering a time comparison goes
    through here.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def to_ist_iso(dt: datetime | str | None) -> str | None:
    """Convert a datetime (or ISO string) to IST (GMT+5:30) and return ISO format string.

    Naive datetimes are assumed UTC (e.g. from MongoDB). Aware datetimes are converted to IST.
    If a string is provided, it is parsed first. Returns None if dt is None.
    """
    if dt is None:
        return None

    if isinstance(dt, str):
        try:
            dt = parse(dt)
        except (ValueError, TypeError):
            return None  # Return None if unparseable

    return assume_utc(dt).astimezone(IST).isoformat()
