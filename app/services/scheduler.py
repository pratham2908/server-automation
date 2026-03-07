"""Scheduling slot engine.

Builds a weekly calendar from the analysis ``best_posting_times`` and assigns
concrete publish datetimes to videos, skipping slots already occupied by
previously scheduled videos.
"""

from datetime import datetime, timedelta
from typing import Optional

import pytz

_DAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def compute_schedule_slots(
    best_posting_times: list[dict],
    occupied_datetimes: list[Optional[datetime]],
    num_videos: int,
    timezone_str: str,
) -> list[datetime]:
    """Return *num_videos* timezone-aware datetimes for upcoming publish slots.

    Parameters
    ----------
    best_posting_times:
        The ``best_posting_times`` array from the analysis document.
        Each entry has ``day_of_week``, ``video_count``, and ``times``.
    occupied_datetimes:
        ``scheduled_at`` values of videos already in the schedule queue.
        ``None`` entries are ignored.
    num_videos:
        How many slots to assign.
    timezone_str:
        IANA timezone string (e.g. ``"Asia/Kolkata"``).

    Returns
    -------
    A sorted list of timezone-aware datetimes, one per video.
    """
    if num_videos <= 0:
        return []

    tz = pytz.timezone(timezone_str)
    now = datetime.now(tz)

    weekly_slots: list[tuple[int, int, int]] = []
    for slot in best_posting_times:
        day_num = _DAY_MAP.get(slot.get("day_of_week", "").lower())
        if day_num is None:
            continue
        for time_str in slot.get("times", []):
            parts = time_str.split(":")
            if len(parts) < 2:
                continue
            h, m = int(parts[0]), int(parts[1])
            weekly_slots.append((day_num, h, m))

    weekly_slots.sort()

    if not weekly_slots:
        return []

    occupied_keys: set[tuple[int, int, int, int, int]] = set()
    for dt in occupied_datetimes:
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        else:
            dt = dt.astimezone(tz)
        occupied_keys.add((dt.year, dt.month, dt.day, dt.hour, dt.minute))

    today = now.date()
    current_weekday = today.weekday()
    week_start = today - timedelta(days=current_weekday)

    assigned: list[datetime] = []
    max_weeks = 52

    for week_offset in range(max_weeks):
        for day_num, hour, minute in weekly_slots:
            slot_date = week_start + timedelta(weeks=week_offset, days=day_num)
            slot_dt = tz.localize(
                datetime(slot_date.year, slot_date.month, slot_date.day, hour, minute)
            )

            if slot_dt <= now:
                continue

            key = (slot_dt.year, slot_dt.month, slot_dt.day, slot_dt.hour, slot_dt.minute)
            if key in occupied_keys:
                continue

            assigned.append(slot_dt)
            occupied_keys.add(key)

            if len(assigned) == num_videos:
                return assigned

    return assigned
