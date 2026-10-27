"""Pure due-time logic for the per-channel auto-sync.

Kept apart from the cron so the schedule can be tested without a database or
a clock: every function takes the channel document and ``now`` as arguments.

Instagram, YouTube and Threads channels ignore ``interval_hours``: they sync
every 15 minutes while any video is in its first week — when its numbers move
fastest and the view curve is worth reading quarter-hour by quarter-hour — and
daily otherwise. The cron works out whether a channel has such a video on every
tick, so publishing one switches the channel to the fast cadence without
anything being stored.

Syncs land on fixed IST clock slots — :00, :15, :30 and :45 while fresh, 00:00
once a day otherwise — instead of counting on from the last run, which used to
drift a minute later every hour. A channel is due when its last sync is older
than the most recent slot, so a server that comes back at :03 runs the missed
:00 sync at once and is back on :15 for the next. A sync costs a YouTube channel about two Data API units
per 50 videos, so even 96 a day stay far inside the 10,000-unit daily quota.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from app.database import get_channel_platform
from app.models.channel import AutoSyncConfig
from app.timezone import IST, assume_utc

# The task name stamped into ``last_tasks`` when an auto-sync starts.
AUTO_SYNC_TASK = "auto_sync"
# A reel counts as fresh, and keeps its channel on 15-minute syncs, for this long after publishing.
FRESH_REEL_DAYS = 7
# Instagram's counts move within minutes (18 minutes added ~1,700 views to a day-old reel), so
# quarter-hour readings show real shape, not repeats of the same number.
FRESH_INTERVAL = timedelta(minutes=15)
IDLE_INTERVAL_HOURS = 24
# Where the slots sit past each interval boundary (from IST midnight). On the quarter-hour itself so
# the readings line up with the clock the charts are drawn on; each reading is stamped when it
# completes, under a minute later, and the charts place a gain by time overlap, so it lands correctly.
SLOT_OFFSET = timedelta(0)


def auto_sync_config(channel: dict[str, Any]) -> AutoSyncConfig:
    """The channel's auto-sync settings, falling back to defaults (off) when absent or malformed.

    PATCH validates this block, but documents written before that validation
    existed — or edited by hand — may not match. Treating an unreadable block as
    "off" is the safe reading: a bad interval must never turn into a sync every tick.
    """
    raw = (channel.get("automation_config") or {}).get("auto_sync")
    if not isinstance(raw, dict):
        return AutoSyncConfig()
    try:
        return AutoSyncConfig.model_validate(raw)
    except ValidationError:
        return AutoSyncConfig()


def sync_interval(channel: dict[str, Any], has_fresh_reel: bool) -> timedelta:
    """Time between this channel's auto-syncs."""
    if get_channel_platform(channel) in ("instagram", "youtube", "threads"):
        return FRESH_INTERVAL if has_fresh_reel else timedelta(hours=IDLE_INTERVAL_HOURS)
    return timedelta(hours=auto_sync_config(channel).interval_hours)


def latest_slot(now: datetime, interval: timedelta) -> datetime:
    """The newest sync slot at or before *now*: IST midnight plus ``SLOT_OFFSET`` plus whole intervals.

    Slots restart at each midnight, so an interval that doesn't divide a day
    still lands on the same clock times every day.
    """
    local = now.astimezone(IST)
    first = local.replace(hour=0, minute=0, second=0, microsecond=0) + SLOT_OFFSET
    if local < first:
        # Before today's first slot (before the first slot): the newest one is yesterday's last.
        first -= timedelta(days=1)
    return first + interval * ((local - first) // interval)


def last_auto_sync_at(channel: dict[str, Any]) -> datetime | None:
    """When this channel's last auto-sync started; ``None`` if it has never run."""
    last = (channel.get("last_tasks") or {}).get(AUTO_SYNC_TASK)
    return assume_utc(last) if isinstance(last, datetime) else None


def is_auto_sync_due(channel: dict[str, Any], now: datetime, has_fresh_reel: bool = False) -> bool:
    """Whether the cron should sync this channel on the tick at ``now``.

    Due-ness is computed from the persisted ``last_tasks.auto_sync`` stamp, not an
    in-process timer, so a server restart neither skips a sync nor fires an extra one:
    a missed slot is caught up once, and the next sync is back on the clock.
    """
    if not auto_sync_config(channel).enabled:
        return False
    last = last_auto_sync_at(channel)
    return last is None or last < latest_slot(now, sync_interval(channel, has_fresh_reel))


def seconds_to_next_minute(now: datetime) -> float:
    """How long the cron sleeps so it wakes just after the minute, where every slot sits.

    Half a second past, because the event loop's clock and the wall clock can disagree
    by a hair; waking a moment early would see :04:59.99 and leave the :05 slot a minute.
    """
    return 60.5 - now.second - now.microsecond / 1_000_000


# An older reel's numbers are re-read about once a day. An hour short of a full
# day so a channel on daily syncs, whose stamp lands a few seconds after the run
# starts, doesn't miss the next day's run by seconds and slip to every other day.
OLD_REEL_REFRESH_HOURS = IDLE_INTERVAL_HOURS - 1


def needs_reel_insights(published_at: datetime | None, last_read: datetime | None, now: datetime) -> bool:
    """Whether an incremental sync should re-read this reel's numbers (one API call)."""
    if published_at is None or last_read is None:
        return True
    if now - assume_utc(published_at) < timedelta(days=FRESH_REEL_DAYS):
        return True
    return now - assume_utc(last_read) >= timedelta(hours=OLD_REEL_REFRESH_HOURS)
