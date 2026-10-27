"""Due-time logic for the per-channel auto-sync."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.routers.channels import ChannelUpdate
from app.services.auto_sync_schedule import (
    FRESH_INTERVAL,
    auto_sync_config,
    is_auto_sync_due,
    latest_slot,
    needs_reel_insights,
    seconds_to_next_minute,
)
from app.timezone import IST

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


def ist(hour: int, minute: int, second: float = 0, day: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=IST) + timedelta(seconds=second)


class TestLatestSlot:
    @pytest.mark.parametrize(
        ("now", "slot"),
        [
            (ist(14, 12), ist(14, 0)),
            (ist(14, 15), ist(14, 15)),
            (ist(14, 14, 59), ist(14, 0)),
            (ist(14, 45, 30), ist(14, 45)),
            # Just after midnight is the day's first slot.
            (ist(0, 2), ist(0, 0)),
        ],
    )
    def test_quarter_hour_slots_sit_at_00_15_30_45(self, now, slot):
        assert latest_slot(now, FRESH_INTERVAL) == slot

    def test_daily_slot_is_midnight(self):
        assert latest_slot(ist(17, 30), timedelta(hours=24)) == ist(0, 0)
        assert latest_slot(ist(0, 3), timedelta(hours=24)) == ist(0, 0)

    def test_reads_utc_input_in_ist(self):
        assert latest_slot(datetime(2026, 10, 12, 8, 40, tzinfo=timezone.utc), FRESH_INTERVAL) == ist(14, 0)


class TestIsAutoSyncDue:
    """A channel without a platform reads as YouTube: daily at 00:00 unless it has a fresh video."""

    def test_disabled_is_never_due(self):
        assert is_auto_sync_due(_channel({"enabled": False}), NOW) is False

    def test_never_run_is_due_immediately(self):
        assert is_auto_sync_due(_channel({"enabled": True, "interval_hours": 6}), NOW) is True

    def test_not_due_once_todays_slot_has_run(self):
        ch = _channel({"enabled": True, "interval_hours": 6}, last=ist(0, 0, 2))
        assert is_auto_sync_due(ch, ist(23, 59)) is False

    def test_due_when_the_last_sync_predates_the_slot(self):
        ch = _channel({"enabled": True, "interval_hours": 6}, last=ist(23, 59, day=11))
        assert is_auto_sync_due(ch, ist(0, 0)) is True

    def test_naive_mongo_timestamp_is_read_as_utc(self):
        # Mongo returns naive UTC: 18:40 UTC on the 11th is 00:10 IST on the 12th, after the
        # 00:00 slot. Read as IST it would look like the 11th and fire again.
        ch = _channel({"enabled": True, "interval_hours": 6}, last=datetime(2026, 10, 11, 18, 40))
        assert is_auto_sync_due(ch, ist(17, 30)) is False


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


class TestInstagramCadence:
    """Instagram and YouTube sync on the :00/:15/:30/:45 slots while a video is in its first week, daily otherwise."""

    def _ig(self, last: datetime) -> dict:
        return {**_channel({"enabled": True, "interval_hours": 6}, last=last), "platform": "instagram"}

    def test_runs_on_each_quarter_slot(self):
        ch = self._ig(ist(14, 0, 1))
        assert is_auto_sync_due(ch, ist(14, 14, 59), has_fresh_reel=True) is False
        assert is_auto_sync_due(ch, ist(14, 15), has_fresh_reel=True) is True

    def test_a_missed_slot_is_caught_up_then_back_on_the_clock(self):
        # Down over 14:00 and back at 14:03: the missed sync runs now...
        assert is_auto_sync_due(self._ig(ist(13, 45, 1)), ist(14, 3), has_fresh_reel=True) is True
        # ...and the one after is still 14:15, not 14:18.
        caught_up = self._ig(ist(14, 3))
        assert is_auto_sync_due(caught_up, ist(14, 14), has_fresh_reel=True) is False
        assert is_auto_sync_due(caught_up, ist(14, 15), has_fresh_reel=True) is True

    def test_daily_without_one_whatever_the_stored_interval(self):
        ch = self._ig(ist(0, 0, 1))
        assert is_auto_sync_due(ch, ist(23, 59), has_fresh_reel=False) is False
        assert is_auto_sync_due(ch, ist(0, 0, day=13), has_fresh_reel=False) is True

    def test_a_new_reel_switches_an_idle_channel_to_15_minutes_at_once(self):
        # Last synced at today's 00:00 on the daily cadence: a reel going up makes it due now.
        assert is_auto_sync_due(self._ig(ist(0, 0, 1)), ist(14, 12), has_fresh_reel=True) is True

    def test_youtube_follows_the_same_cadence(self):
        ch = {**_channel({"enabled": True, "interval_hours": 6}, last=ist(13, 45, 1)), "platform": "youtube"}
        assert is_auto_sync_due(ch, ist(14, 0), has_fresh_reel=True) is True
        assert is_auto_sync_due(ch, ist(14, 0), has_fresh_reel=False) is False


def test_cron_wakes_just_after_the_minute():
    assert seconds_to_next_minute(ist(14, 14, 59.8)) == pytest.approx(0.7)
    assert seconds_to_next_minute(ist(14, 15, 0.5)) == pytest.approx(60.0)


class TestNeedsReelInsights:
    def test_a_fresh_reel_is_read_every_time(self):
        assert needs_reel_insights(NOW - timedelta(days=6), NOW - timedelta(minutes=5), NOW) is True

    def test_an_older_reel_is_read_about_once_a_day(self):
        published = NOW - timedelta(days=30)
        assert needs_reel_insights(published, NOW - timedelta(hours=5), NOW) is False
        # An hour of slack, so a daily run starting seconds early still reads it.
        assert needs_reel_insights(published, NOW - timedelta(hours=23, minutes=59, seconds=50), NOW) is True

    def test_a_reel_never_read_or_without_a_date_is_read(self):
        assert needs_reel_insights(NOW - timedelta(days=30), None, NOW) is True
        assert needs_reel_insights(None, NOW, NOW) is True
