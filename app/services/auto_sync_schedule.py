"""Pure due-time logic for the per-channel auto-sync.

Kept apart from the cron so the schedule can be tested without a database or
a clock: every function takes the channel document and ``now`` as arguments.

Instagram, YouTube and Threads channels ignore ``interval_hours``: they sync
every 15 minutes while any video is in its first week — when its numbers move
fastest and the view curve is worth reading quarter-hour by quarter-hour — and
daily otherwise. The cron works out whether a channel has such a video on every
tick, so publishing one switches the channel to the fast cadence without
anything being stored. A sync costs a YouTube channel about two Data API units
per 50 videos, so even 96 a day stay far inside the 10,000-unit daily quota.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from app.database import get_channel_platform
from app.models.channel import AutoSyncConfig
from app.timezone import assume_utc

# The task name stamped into ``last_tasks`` when an auto-sync starts.
AUTO_SYNC_TASK = "auto_sync"
# A reel counts as fresh, and keeps its channel on 15-minute syncs, for this long after publishing.
FRESH_REEL_DAYS = 7
# Instagram's counts move within minutes (18 minutes added ~1,700 views to a day-old reel), so
# quarter-hour readings show real shape, not repeats of the same number.
FRESH_INTERVAL = timedelta(minutes=15)
IDLE_INTERVAL_HOURS = 24


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


def next_auto_sync_at(channel: dict[str, Any], interval: timedelta) -> datetime | None:
    """When this channel's next auto-sync falls due; ``None`` if it has never run."""
    last = (channel.get("last_tasks") or {}).get(AUTO_SYNC_TASK)
    if not isinstance(last, datetime):
        return None
    return assume_utc(last) + interval


def is_auto_sync_due(channel: dict[str, Any], now: datetime, has_fresh_reel: bool = False) -> bool:
    """Whether the cron should sync this channel on the tick at ``now``.

    Due-ness is computed from the persisted ``last_tasks.auto_sync`` stamp, not an
    in-process timer, so a server restart neither skips a sync nor fires an extra one.
    """
    if not auto_sync_config(channel).enabled:
        return False
    due_at = next_auto_sync_at(channel, sync_interval(channel, has_fresh_reel))
    return due_at is None or due_at <= now


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
