"""Pure due-time logic for the per-channel auto-sync.

Kept apart from the cron so the schedule can be tested without a database or
a clock: every function takes the channel document and ``now`` as arguments.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from app.models.channel import AutoSyncConfig
from app.timezone import assume_utc

# The task name stamped into ``last_tasks`` when an auto-sync starts.
AUTO_SYNC_TASK = "auto_sync"


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


def next_auto_sync_at(channel: dict[str, Any]) -> datetime | None:
    """When this channel's next auto-sync falls due; ``None`` if it has never run."""
    last = (channel.get("last_tasks") or {}).get(AUTO_SYNC_TASK)
    if not isinstance(last, datetime):
        return None
    return assume_utc(last) + timedelta(hours=auto_sync_config(channel).interval_hours)


def is_auto_sync_due(channel: dict[str, Any], now: datetime) -> bool:
    """Whether the cron should sync this channel on the tick at ``now``.

    Due-ness is computed from the persisted ``last_tasks.auto_sync`` stamp, not an
    in-process timer, so a server restart neither skips a sync nor fires an extra one.
    """
    if not auto_sync_config(channel).enabled:
        return False
    due_at = next_auto_sync_at(channel)
    return due_at is None or due_at <= now
