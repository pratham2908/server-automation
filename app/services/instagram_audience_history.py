"""One demographic breakdown across the stored days, shaped for a time-series chart.

Pure: no I/O. ``instagram_audience`` holds one document per channel per day; this picks one
audience × timeframe × breakdown out of each and flags the days Instagram's figures moved.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.services.instagram_audience import FOLLOWER_TIMEFRAME


def _rows(doc: Mapping[str, Any], audience: str, timeframe: str, breakdown: str) -> list[dict[str, Any]]:
    # Followers are stored under every timeframe with identical numbers, but asked for only one.
    frame = FOLLOWER_TIMEFRAME if audience == "followers" else timeframe
    found = ((doc.get("audiences") or {}).get(audience) or {}).get(frame) or {}
    return [{"value": str(r["value"]), "count": int(r["count"])} for r in found.get(breakdown) or []]


def history_days(
    docs: Iterable[Mapping[str, Any]], audience: str, timeframe: str, breakdown: str
) -> list[dict[str, Any]]:
    """``[{date, fetched_at, changed, rows}]`` oldest first.

    ``changed`` is False when the rows equal the previous day's: Instagram refreshes these figures
    only every day or three, so a repeat is a stale read, not a day on which nothing moved.
    The first day is always ``changed``. Days with no rows for the selection are dropped.
    """
    out: list[dict[str, Any]] = []
    previous: list[dict[str, Any]] | None = None
    for doc in sorted(docs, key=lambda d: str(d.get("date"))):
        rows = _rows(doc, audience, timeframe, breakdown)
        if not rows:
            continue
        out.append(
            {
                "date": str(doc["date"]),
                "fetched_at": doc.get("fetched_at"),
                "changed": previous is None or rows != previous,
                "rows": rows,
            }
        )
        previous = rows
    return out
