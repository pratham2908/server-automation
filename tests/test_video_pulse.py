from __future__ import annotations

from app.services.video_pulse import PULSE_KEYS, pulse_filters


def test_every_headline_count_has_a_filter_scoped_to_the_channel() -> None:
    filters = pulse_filters("zara kapoor")
    assert tuple(filters) == PULSE_KEYS
    assert all(f["channel_id"] == "zara kapoor" for f in filters.values())


def test_unverified_counts_a_missing_verification_status() -> None:
    assert pulse_filters("c")["unverified"]["verification_status"] == {"$ne": "verified"}


def test_attention_ignores_archived_videos() -> None:
    assert pulse_filters("c")["attention"]["status"] == {"$ne": "archived"}
