"""Pure selection logic for the daily auto-scheduler.

No I/O here — slot timing, the today-commitment count, and which video to pick
from Ready or from an import source.
"""

from datetime import date, datetime, time, timedelta

import app.services.auto_scheduler_selection as sel
from app.models.video_source import SourceVideo
from app.services.auto_scheduler_selection import (
    due_slots,
    elapsed_seconds,
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


# ------------------------------------------------------------------
# Generation — asking an app to render something new
# ------------------------------------------------------------------


def _slot_at(hh, mm=0):
    return datetime(2026, 8, 24, hh, mm, tzinfo=IST)


def test_generation_fits_slot_uses_eta_plus_import_slack():
    slot = _slot_at(19)
    # The cron acts an hour ahead, which is room enough for every app we have.
    assert sel.generation_fits_slot(slot - timedelta(minutes=60), slot, 15) is True
    assert sel.generation_fits_slot(slot - timedelta(minutes=60), slot, 30) is True
    # A 30-minute render with 5 minutes of import slack needs 35 minutes.
    assert sel.generation_fits_slot(slot - timedelta(minutes=35), slot, 30) is True
    assert sel.generation_fits_slot(slot - timedelta(minutes=34), slot, 30) is False


def test_pick_generation_source_prefers_configured_order_then_falls_back_to_a_faster_app():
    slot = _slot_at(19)
    blender = sel.GenerationCandidate("b", "Music Blender", eta_minutes=30, max_per_day=4)
    georank = sel.GenerationCandidate("g", "GeoRank", eta_minutes=15, max_per_day=4)
    candidates = [blender, georank]

    # Plenty of time: the operator's first choice wins.
    assert sel.pick_generation_source(candidates, slot - timedelta(minutes=60), slot) is blender
    # Too late for the slow one, still fine for the quick one.
    assert sel.pick_generation_source(candidates, slot - timedelta(minutes=25), slot) is georank
    # Too late for anything.
    assert sel.pick_generation_source(candidates, slot - timedelta(minutes=10), slot) is None


def test_may_request_generation_respects_both_caps():
    assert sel.may_request_generation(0, 4, 0, 2) is True
    assert sel.may_request_generation(3, 4, 1, 2) is True
    assert sel.may_request_generation(4, 4, 0, 2) is False  # daily cap reached
    assert sel.may_request_generation(0, 4, 2, 2) is False  # too many already rendering


def test_generation_expires_at_the_slot_it_was_meant_to_fill():
    slot = _slot_at(19)
    assert sel.generation_expired(slot - timedelta(minutes=1), slot) is False
    assert sel.generation_expired(slot, slot) is True


def test_generating_slots_count_as_in_flight_so_we_do_not_over_post():
    """A render in flight is a commitment-to-be, exactly like an import.

    Without counting it, an external commitment plus a pending slot would let the
    channel post more times than it has slots.
    """
    now = _slot_at(19, 30)
    times = ["18:00", "19:00"]

    # One slot rendering, one external commitment already today: nothing left to do.
    assert sel.pending_action_slots(times, now, 1, {"18:00": "generating"}) == []
    # Same shape with an import in flight behaves identically.
    assert sel.pending_action_slots(times, now, 1, {"18:00": "importing"}) == []
    # With nothing in flight the second slot is still fillable.
    assert sel.pending_action_slots(times, now, 1, {}) == ["18:00"]


# ------------------------------------------------------------------
# Naive Mongo timestamps
# ------------------------------------------------------------------


def test_a_naive_mongo_timestamp_is_read_as_utc_not_relabelled_ist():
    """The bug this pins: a naive value was given IST's offset instead of being
    converted from UTC, moving every stored instant 5h30m into the past.

    A video queued for 23:00 IST is stored 17:30 UTC. Relabelling that as IST put
    it at 17:30 IST — same date here, but the same misread on an elapsed-time
    comparison is what made a wait look exhausted before it had begun.
    """
    stored_utc = datetime(2026, 9, 25, 17, 30)  # 23:00 IST, as Mongo hands it back
    assert sel._local_date(stored_utc) == date(2026, 9, 25)

    # Just after midnight IST belongs to the new day, though UTC still says the old.
    just_after_midnight_ist = datetime(2026, 9, 25, 19, 0)  # 00:30 IST on the 26th
    assert sel._local_date(just_after_midnight_ist) == date(2026, 9, 26)


def test_committed_today_counts_a_late_evening_slot_on_its_own_day():
    """The practical cost of the misread: a 23:00 IST post is stored as 17:30 UTC,
    and reading that as IST would still land on the right date — but a 00:30 IST
    one would be credited to yesterday, so the channel would post twice."""
    videos = [{"status": "queued", "scheduled_at": datetime(2026, 9, 25, 19, 0)}]  # 00:30 IST on the 26th
    assert videos_committed_today(videos, date(2026, 9, 26)) == 1
    assert videos_committed_today(videos, date(2026, 9, 25)) == 0


def test_as_datetime_is_always_aware_so_picks_cannot_raise():
    """``pick_ready_video`` compares parsed timestamps against an aware sentinel.
    One naive value in that mix raises TypeError rather than sorting oddly, so a
    single video missing ``created_at`` would break the whole pick."""
    videos = [
        {"video_id": "no-date", "status": "ready"},
        {"video_id": "naive", "status": "ready", "created_at": datetime(2026, 9, 25, 1, 0)},
        {"video_id": "aware", "status": "ready", "created_at": "2026-09-25T09:00:00+05:30"},
    ]
    assert pick_ready_video(videos)["video_id"] == "naive"  # 06:30 IST, the oldest


def test_elapsed_seconds_spans_naive_and_aware_without_the_ist_skew():
    """Mongo hands back naive UTC and the scheduler works in aware IST, so both
    sides of a duration routinely have different awareness. Mixing them raises
    TypeError; relabelling instead of converting would report a 5h30m error."""
    started_naive = datetime(2026, 9, 24, 13, 3, 22)  # 18:33:22 IST, as stored
    finished_aware = datetime(2026, 9, 24, 18, 42, 26, tzinfo=sel.IST)
    assert elapsed_seconds(started_naive, finished_aware) == 544.0
    # Same instants, both naive — the everyday case once both came from Mongo.
    assert elapsed_seconds(started_naive, datetime(2026, 9, 24, 13, 12, 26)) == 544.0


def test_elapsed_seconds_is_none_when_it_cannot_be_known():
    now = datetime(2026, 9, 24, 13, 0, 0)
    assert elapsed_seconds(None, now) is None
    assert elapsed_seconds(now, None) is None
    assert elapsed_seconds("not a date", now) is None
    # Written out of order (a clock step, or a re-run): None beats "0s", which
    # would read as an instant render.
    assert elapsed_seconds(now, now - timedelta(seconds=30)) is None
    assert elapsed_seconds(now, now) == 0.0  # genuinely simultaneous is still a fact


def test_iso_strings_are_accepted_so_either_storage_shape_measures():
    assert elapsed_seconds("2026-09-24T13:03:22", "2026-09-24T13:12:26") == 544.0
