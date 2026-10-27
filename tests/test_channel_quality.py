"""A channel's skip rate and watch time from per-reel figures."""

from __future__ import annotations

import pytest

from app.services.channel_quality import channel_level, channel_quality_sums

REELS = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
INSIGHTS = {
    # 1,000 views, 40% skipped, 10,000s watched.
    "a": {"views": 1000, "reels_skip_rate": 40.0, "ig_reels_video_view_total_time": 10_000_000},
    # 3,000 views, 60% skipped, 6,000s watched.
    "b": {"views": 3000, "reels_skip_rate": 60.0, "ig_reels_video_view_total_time": 6_000_000},
    # No skip figure and no views: left out of everything.
    "c": {"views": 0},
}
DURATIONS = {"a": 20.0, "b": 30.0}


def test_skip_rate_is_weighted_by_views() -> None:
    sums = channel_quality_sums(REELS, INSIGHTS, DURATIONS)
    # (1000*.4 + 3000*.6) / 4000 = 55%
    assert channel_level(sums, "skip_rate") == pytest.approx(55.0)


def test_average_watch_time_is_total_watched_over_views() -> None:
    sums = channel_quality_sums(REELS, INSIGHTS, DURATIONS)
    assert channel_level(sums, "avg_watch_seconds") == pytest.approx(16_000 / 4000)


def test_watch_through_is_watched_share_of_length() -> None:
    sums = channel_quality_sums(REELS, INSIGHTS, DURATIONS)
    # (10000/20 + 6000/30) / 4000 = 700 / 4000
    assert channel_level(sums, "watch_through") == pytest.approx(0.175)


def test_a_reel_without_a_length_still_counts_for_watch_time_but_not_watch_through() -> None:
    sums = channel_quality_sums(REELS, INSIGHTS, {"a": 20.0})
    assert channel_level(sums, "avg_watch_seconds") == pytest.approx(4.0)
    assert channel_level(sums, "watch_through") == pytest.approx(10_000 / 20 / 1000)


def test_unrecorded_or_unknown_is_none() -> None:
    assert channel_level({"views": 5}, "skip_rate") is None
    assert channel_level({"skipped_views": 1.0, "skip_base": 0.0}, "skip_rate") is None
    assert channel_level({}, "views") is None
