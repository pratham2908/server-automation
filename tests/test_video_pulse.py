from __future__ import annotations

from app.services.video_pulse import PULSE_KEYS, pulse_filters


def test_every_headline_count_has_a_filter_scoped_to_the_channel() -> None:
    filters = pulse_filters("zara kapoor")
    assert tuple(filters) == PULSE_KEYS
    assert all(f["channel_id"] == "zara kapoor" for f in filters.values())


def test_attention_ignores_archived_videos() -> None:
    assert pulse_filters("c")["attention"]["status"] == {"$ne": "archived"}


def test_queued_instagram_reels_count_as_scheduled():
    from tests.mongo_fakes import matches

    scheduled = pulse_filters("c")["scheduled"]
    assert matches({"channel_id": "c", "status": "queued"}, scheduled)
    assert matches({"channel_id": "c", "status": "scheduled"}, scheduled)
    assert not matches({"channel_id": "c", "status": "ready"}, scheduled)
