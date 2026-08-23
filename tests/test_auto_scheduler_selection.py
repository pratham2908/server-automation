"""Pure selection logic for the daily auto-scheduler.

No I/O here — slot timing, the today-commitment count, and which video to pick
from Ready or from an import source.
"""

from datetime import date, datetime, time

from app.models.video_source import SourceVideo
from app.services.auto_scheduler_selection import (
    due_slots,
    parse_slot,
    pending_action_slots,
    pick_ready_video,
    pick_source_video,
    recheck_ready,
    slot_datetimes,
    videos_committed_today,
    wait_exhausted,
)
from app.timezone import IST


def _dt(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=IST)


# ------------------------------------------------------------------
# slot timing
# ------------------------------------------------------------------


def test_parse_slot_reads_hh_mm():
    assert parse_slot("19:00") == time(19, 0)
    assert parse_slot("07:30") == time(7, 30)


def test_slot_runs_one_hour_before_it_schedules():
    run_at, schedule_at = slot_datetimes("19:00", date(2026, 8, 24))
    assert schedule_at == _dt(2026, 8, 24, 19, 0)
    assert run_at == _dt(2026, 8, 24, 18, 0)


def test_due_slots_are_those_whose_run_time_has_arrived():
    times = ["19:00", "21:00"]
    # 18:30 → only the 19:00 slot's run window (18:00) has opened
    assert due_slots(times, _dt(2026, 8, 24, 18, 30)) == ["19:00"]
    # 20:15 → both (19:00 ran at 18:00, 21:00 runs at 20:00)
    assert due_slots(times, _dt(2026, 8, 24, 20, 15)) == ["19:00", "21:00"]
    # 17:00 → nothing yet
    assert due_slots(times, _dt(2026, 8, 24, 17, 0)) == []


# ------------------------------------------------------------------
# which due slots still need a video (frequency + in-flight accounting)
# ------------------------------------------------------------------


def test_pending_slots_acts_on_a_due_slot_when_nothing_committed():
    # 18:30 → 19:00's run window (18:00) is open; nothing done yet.
    assert pending_action_slots(["19:00", "21:00"], _dt(2026, 8, 24, 18, 30), 0, {}) == ["19:00"]


def test_pending_slots_stops_when_the_days_pace_is_already_met():
    # One video already committed today satisfies the single due slot.
    assert pending_action_slots(["19:00", "21:00"], _dt(2026, 8, 24, 18, 30), 1, {}) == []


def test_pending_slots_counts_an_in_flight_import_as_covering_a_slot():
    # 20:15 → both slots due; one already scheduled, the other importing → nothing new.
    states = {"19:00": "scheduled", "21:00": "importing"}
    # committed_today = 1 (the scheduled one); importing covers the second.
    assert pending_action_slots(["19:00", "21:00"], _dt(2026, 8, 24, 20, 15), 1, states) == []


def test_pending_slots_skips_already_handled_slots_and_returns_the_open_one():
    states = {"19:00": "scheduled"}
    assert pending_action_slots(["19:00", "21:00"], _dt(2026, 8, 24, 20, 15), 1, states) == ["21:00"]


def test_pending_slots_ignores_slots_whose_time_has_not_come():
    assert pending_action_slots(["19:00", "21:00"], _dt(2026, 8, 24, 17, 0), 0, {}) == []


# ------------------------------------------------------------------
# import recheck timing
# ------------------------------------------------------------------


def test_recheck_ready_only_after_the_recheck_window():
    started = _dt(2026, 8, 24, 18, 0)
    assert recheck_ready(started, _dt(2026, 8, 24, 18, 20), 35) is False
    assert recheck_ready(started, _dt(2026, 8, 24, 18, 40), 35) is True


def test_wait_exhausted_after_the_max_wait():
    started = _dt(2026, 8, 24, 18, 0)
    assert wait_exhausted(started, _dt(2026, 8, 24, 19, 0), 90) is False
    assert wait_exhausted(started, _dt(2026, 8, 24, 19, 40), 90) is True


# ------------------------------------------------------------------
# today's commitment count (frequency guard)
# ------------------------------------------------------------------


def test_committed_today_counts_published_and_scheduled_for_the_day():
    day = date(2026, 8, 24)
    videos = [
        {"status": "published", "published_at": _dt(2026, 8, 24, 9, 0)},
        {"status": "scheduled", "scheduled_at": _dt(2026, 8, 24, 21, 0)},
        {"status": "queued", "scheduled_at": _dt(2026, 8, 24, 19, 0)},
        {"status": "published", "published_at": _dt(2026, 8, 23, 9, 0)},  # yesterday — excluded
        {"status": "ready"},  # not committed
    ]
    assert videos_committed_today(videos, day) == 3


def test_committed_today_tolerates_iso_string_timestamps():
    day = date(2026, 8, 24)
    videos = [{"status": "scheduled", "scheduled_at": "2026-08-24T19:00:00+05:30"}]
    assert videos_committed_today(videos, day) == 1


# ------------------------------------------------------------------
# pick from Ready — FIFO oldest
# ------------------------------------------------------------------


def test_pick_ready_returns_the_oldest_ready_video():
    videos = [
        {"video_id": "b", "status": "ready", "created_at": _dt(2026, 8, 20)},
        {"video_id": "a", "status": "ready", "created_at": _dt(2026, 8, 18)},
        {"video_id": "c", "status": "ready", "created_at": _dt(2026, 8, 22)},
    ]
    assert pick_ready_video(videos)["video_id"] == "a"


def test_pick_ready_ignores_non_ready_videos():
    videos = [
        {"video_id": "x", "status": "queued", "created_at": _dt(2026, 8, 10)},
        {"video_id": "y", "status": "ready", "created_at": _dt(2026, 8, 21)},
    ]
    assert pick_ready_video(videos)["video_id"] == "y"


def test_pick_ready_returns_none_when_nothing_is_ready():
    assert pick_ready_video([{"video_id": "x", "status": "queued"}]) is None


# ------------------------------------------------------------------
# pick from an import source
# ------------------------------------------------------------------


def _sv(id, created, *, status="completed", sent=False, imported=False, group=None):
    return SourceVideo(
        id=id,
        title=id,
        status=status,
        created_at=created,
        already_sent_to_channel=sent,
        imported=imported,
        group_id=group,
    )


def test_ungrouped_source_picks_the_oldest_importable_video():
    # Georank-style: no groups.
    videos = [
        _sv("new", "2026-08-22T00:00:00+05:30"),
        _sv("old", "2026-08-19T00:00:00+05:30"),
        _sv("mid", "2026-08-21T00:00:00+05:30"),
    ]
    assert pick_source_video(videos).id == "old"


def test_ungrouped_skips_sent_imported_and_unfinished():
    videos = [
        _sv("sent", "2026-08-10T00:00:00+05:30", sent=True),
        _sv("imported", "2026-08-11T00:00:00+05:30", imported=True),
        _sv("rendering", "2026-08-12T00:00:00+05:30", status="processing"),
        _sv("good", "2026-08-20T00:00:00+05:30"),
    ]
    assert pick_source_video(videos).id == "good"


def test_grouped_source_uses_episode_rules():
    # VidForge-style: episodes via group_id.
    # ep1: has a sent video -> disqualified entirely.
    # ep2: no sent video -> qualifying; its date (earliest video) is 08-18.
    # ep3: no sent video -> qualifying; its date is 08-20 (newer than ep2).
    videos = [
        _sv("ep1-a", "2026-08-15T00:00:00+05:30", group="ep1", sent=True),
        _sv("ep1-b", "2026-08-16T00:00:00+05:30", group="ep1"),
        _sv("ep2-a", "2026-08-18T00:00:00+05:30", group="ep2"),
        _sv("ep2-b", "2026-08-19T00:00:00+05:30", group="ep2"),  # latest in ep2
        _sv("ep3-a", "2026-08-20T00:00:00+05:30", group="ep3"),
    ]
    # oldest qualifying episode is ep2; within it, pick the latest video (ep2-b).
    assert pick_source_video(videos).id == "ep2-b"


def test_grouped_source_returns_none_when_every_episode_already_posted():
    videos = [
        _sv("ep1-a", "2026-08-15T00:00:00+05:30", group="ep1", sent=True),
        _sv("ep1-b", "2026-08-16T00:00:00+05:30", group="ep1"),
    ]
    assert pick_source_video(videos) is None


def test_source_pick_returns_none_when_there_are_no_videos():
    assert pick_source_video([]) is None
