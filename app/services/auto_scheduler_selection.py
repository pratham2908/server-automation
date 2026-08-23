"""Pure selection logic for the daily auto-scheduler.

No database or network here — just the decisions: when a slot is due, how many
videos a channel has already committed today, and which video to take from Ready
or from an import source. Kept pure so the branching rules are testable directly.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from dateutil.parser import isoparse

from app.models.video_source import SourceVideo
from app.timezone import IST

# How long before a slot's scheduled time its cron pass runs.
_RUN_LEAD = timedelta(hours=1)

# A far-future sentinel so videos with an unknown timestamp sort last for
# "oldest first" and are never treated as the oldest candidate.
_FAR_FUTURE = datetime.max.replace(tzinfo=IST)


def parse_slot(slot: str) -> time:
    """Parse an ``"HH:MM"`` slot string into a ``time``."""
    hh, mm = slot.split(":")
    return time(int(hh), int(mm))


def slot_datetimes(slot: str, day: date) -> tuple[datetime, datetime]:
    """Return ``(run_at, schedule_at)`` in IST for ``slot`` on ``day``.

    ``run_at`` is one hour before ``schedule_at`` — the cron acts then and
    schedules the video for the slot time.
    """
    schedule_at = datetime.combine(day, parse_slot(slot), tzinfo=IST)
    return schedule_at - _RUN_LEAD, schedule_at


def due_slots(schedule_times: list[str], now: datetime) -> list[str]:
    """Slots whose run window has opened (``run_at <= now``) on ``now``'s day."""
    day = now.date()
    return [slot for slot in schedule_times if slot_datetimes(slot, day)[0] <= now]


def pending_action_slots(
    schedule_times: list[str],
    now: datetime,
    committed_today: int,
    slot_states: dict[str, str],
) -> list[str]:
    """Which due slots still need a video picked, in slot order.

    Accounts for the day's whole picture so we never over- or under-post:

    * ``committed_today`` already counts anything we scheduled this run (those
      videos are ``queued``/``scheduled`` now) plus external commitments.
    * ``importing`` slots have a transfer in flight that will become a commitment
      but is not counted yet, so we add it to the projection.

    We top up only to the number of slots whose time has come, and only act on
    slots not already handled (``pending`` or unseen).
    """
    due = due_slots(schedule_times, now)
    importing = sum(1 for s in due if slot_states.get(s) == "importing")
    needed = len(due) - (committed_today + importing)
    if needed <= 0:
        return []
    pending = [s for s in due if slot_states.get(s) in (None, "pending")]
    return pending[:needed]


def recheck_ready(awaiting_since: datetime, now: datetime, recheck_minutes: int) -> bool:
    """Whether enough time has passed since an import was triggered to re-check it."""
    return now - awaiting_since >= timedelta(minutes=recheck_minutes)


def wait_exhausted(awaiting_since: datetime, now: datetime, max_wait_minutes: int) -> bool:
    """Whether an awaited import has taken longer than we are willing to wait."""
    return now - awaiting_since >= timedelta(minutes=max_wait_minutes)


def _as_datetime(value: Any) -> datetime | None:
    """Coerce a Mongo datetime or ISO string to a datetime; ``None`` if absent/bad."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return isoparse(value)
        except (ValueError, TypeError):
            return None
    return None


def _local_date(value: Any) -> date | None:
    dt = _as_datetime(value)
    if dt is None:
        return None
    # Naive timestamps from Mongo are stored UTC; compare in IST like the rest of
    # the app so "today" means the operator's day.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).date()


def videos_committed_today(videos: list[dict], day: date) -> int:
    """Count videos already published on, or scheduled/queued for, ``day``.

    This is the frequency guard: a channel wanting N videos a day is satisfied
    once this reaches N, whether those came from us, a manual schedule, or the
    velocity booster.
    """
    count = 0
    for v in videos:
        status = v.get("status")
        if status == "published" and _local_date(v.get("published_at")) == day:
            count += 1
        elif status in ("scheduled", "queued") and _local_date(v.get("scheduled_at")) == day:
            count += 1
    return count


def pick_ready_video(videos: list[dict]) -> dict | None:
    """Oldest ``ready`` video by ``created_at`` (FIFO); ``None`` if none are ready."""
    ready = [v for v in videos if v.get("status") == "ready"]
    if not ready:
        return None
    return min(ready, key=lambda v: _as_datetime(v.get("created_at")) or _FAR_FUTURE)


def _is_importable(v: SourceVideo) -> bool:
    return v.status == "completed" and not v.already_sent_to_channel and not v.imported


def _created(v: SourceVideo) -> datetime:
    return _as_datetime(v.created_at) or _FAR_FUTURE


def pick_source_video(videos: list[SourceVideo]) -> SourceVideo | None:
    """Choose one source video to import.

    Ungrouped sources (e.g. GeoRank): the oldest importable video.

    Grouped sources (e.g. VidForge, grouped by episode):
      1. A *qualifying* episode is one where no video has been sent to the channel.
      2. Among qualifying episodes, take the oldest (by its earliest video's date).
      3. Within that episode, take the latest video.
    """
    grouped = any(v.group_id is not None for v in videos)

    if not grouped:
        candidates = [v for v in videos if _is_importable(v)]
        return min(candidates, key=_created) if candidates else None

    # Episodes containing any already-sent video are disqualified outright.
    disqualified = {v.group_id for v in videos if v.already_sent_to_channel}

    episodes: dict[str, list[SourceVideo]] = defaultdict(list)
    for v in videos:
        if v.group_id is not None and v.group_id not in disqualified and _is_importable(v):
            episodes[v.group_id].append(v)
    if not episodes:
        return None

    # Oldest qualifying episode by its earliest video, then that episode's latest video.
    oldest_group = min(episodes, key=lambda g: min(_created(v) for v in episodes[g]))
    return max(episodes[oldest_group], key=_created)
