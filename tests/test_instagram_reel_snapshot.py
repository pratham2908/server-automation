"""What a sync keeps about a reel, and when a trial reel is noticed joining the grid."""

from __future__ import annotations

from datetime import datetime, timezone

from app.services.instagram_reel_snapshot import graduation_fields, snapshot_values

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
EARLIER = datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)

# --- the reading --------------------------------------------------------------


def test_a_reading_keeps_every_tracked_number():
    meta = {
        "views": 100,
        "likes": 5,
        "comments": 1,
        "shares": 2,
        "saves": 3,
        "reach": 80,
        "skip_rate": 54.3,
        "avg_view_duration_seconds": 6.94,
        "total_watch_time_seconds": 694.0,
        "engagement_rate": 9.9,
    }
    values = snapshot_values(meta, True)
    assert values == {
        "views": 100,
        "likes": 5,
        "comments": 1,
        "shares": 2,
        "saves": 3,
        "reach": 80.0,
        "skip_rate": 54.3,
        "avg_view_duration_seconds": 6.94,
        "total_watch_time_seconds": 694.0,
        "shared_to_feed": True,
    }


def test_a_number_instagram_did_not_report_is_left_out_not_zeroed():
    values = snapshot_values({"views": 10, "likes": 1}, None)
    assert "skip_rate" not in values and "reach" not in values and "shared_to_feed" not in values


def test_skip_rate_keeps_its_decimals():
    assert snapshot_values({"views": 1, "skip_rate": 41.3}, None)["skip_rate"] == 41.3


# --- graduation ---------------------------------------------------------------


def _trial(**over):
    doc = {"trial_graduation": "MANUAL", "shared_to_feed": False}
    doc.update(over)
    return doc


def test_a_trial_that_joins_the_grid_is_stamped_with_when_it_was_noticed():
    fields = graduation_fields(_trial(), True, NOW, EARLIER)
    assert fields["graduated_at"] == NOW
    assert fields["graduated_after"] == EARLIER
    assert fields["graduated_detected_by"] == "grid"
    assert fields["shared_to_feed"] is True


def test_a_trial_still_off_the_grid_is_not_graduated():
    fields = graduation_fields(_trial(), False, NOW, EARLIER)
    assert fields == {"shared_to_feed": False}


def test_a_trial_first_seen_already_on_the_grid_cannot_be_dated():
    """It may have been graduated any time before we first looked."""
    fields = graduation_fields(_trial(shared_to_feed=None), True, NOW, None)
    assert fields.get("graduation_time_unknown") is True
    assert "graduated_at" not in fields


def test_a_normal_reel_off_the_grid_is_never_a_graduation():
    """is_shared_to_feed says nothing about trials: a normal reel can be off the grid too."""
    doc = {"shared_to_feed": False}
    fields = graduation_fields(doc, True, NOW, EARLIER)
    assert fields == {"shared_to_feed": True}


def test_the_first_graduation_time_is_kept():
    fields = graduation_fields(_trial(graduated_at=EARLIER, shared_to_feed=True), True, NOW, EARLIER)
    assert "graduated_at" not in fields


def test_a_reel_removed_from_the_grid_later_does_not_clear_the_time():
    fields = graduation_fields(_trial(graduated_at=EARLIER, shared_to_feed=True), False, NOW, EARLIER)
    assert fields == {"shared_to_feed": False}


def test_nothing_is_written_when_instagram_did_not_say():
    assert graduation_fields(_trial(), None, NOW, EARLIER) == {}
