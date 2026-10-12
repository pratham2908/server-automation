"""Due-time logic for the per-channel auto-sync."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.routers.channels import ChannelUpdate
from app.services.auto_sync_schedule import auto_sync_config, is_auto_sync_due, next_auto_sync_at

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def _channel(auto_sync: object = None, last: datetime | None = None) -> dict:
    doc: dict = {"channel_id": "ch1", "automation_config": {}}
    if auto_sync is not None:
        doc["automation_config"]["auto_sync"] = auto_sync
    if last is not None:
        doc["last_tasks"] = {"auto_sync": last}
    return doc


class TestAutoSyncConfig:
    def test_absent_block_is_off(self):
        assert auto_sync_config({"channel_id": "ch1"}).enabled is False

    @pytest.mark.parametrize(
        "bad", [{"enabled": True, "interval_hours": 0}, {"enabled": True, "interval_hours": "x"}, "on"]
    )
    def test_malformed_block_is_off_not_every_tick(self, bad):
        assert auto_sync_config(_channel(bad)).enabled is False

    def test_reads_interval(self):
        assert auto_sync_config(_channel({"enabled": True, "interval_hours": 6})).interval_hours == 6


class TestIsAutoSyncDue:
    def test_disabled_is_never_due(self):
        assert is_auto_sync_due(_channel({"enabled": False}), NOW) is False

    def test_never_run_is_due_immediately(self):
        assert is_auto_sync_due(_channel({"enabled": True, "interval_hours": 6}), NOW) is True

    def test_not_due_inside_interval(self):
        ch = _channel({"enabled": True, "interval_hours": 6}, last=NOW - timedelta(hours=5, minutes=59))
        assert is_auto_sync_due(ch, NOW) is False

    def test_due_once_interval_elapses(self):
        ch = _channel({"enabled": True, "interval_hours": 6}, last=NOW - timedelta(hours=6))
        assert is_auto_sync_due(ch, NOW) is True

    def test_naive_mongo_timestamp_is_read_as_utc(self):
        # Mongo returns naive UTC. Read as IST it would look 5h30 older and fire early.
        naive = (NOW - timedelta(hours=1)).replace(tzinfo=None)
        ch = _channel({"enabled": True, "interval_hours": 6}, last=naive)
        assert next_auto_sync_at(ch) == NOW + timedelta(hours=5)
        assert is_auto_sync_due(ch, NOW) is False


class TestChannelUpdateValidation:
    def test_rejects_zero_interval(self):
        with pytest.raises(ValueError):
            ChannelUpdate(automation_config={"auto_sync": {"enabled": True, "interval_hours": 0}})

    def test_fills_defaults_and_keeps_other_blocks(self):
        body = ChannelUpdate(automation_config={"auto_sync": {"enabled": True}, "auto_scheduler": {"enabled": False}})
        assert body.automation_config == {
            "auto_sync": {"enabled": True, "interval_hours": 6},
            "auto_scheduler": {"enabled": False},
        }
