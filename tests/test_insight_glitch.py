"""Zero-view glitch detection."""

from __future__ import annotations

from app.services.insight_glitch import regressed_reels


def test_zero_after_views_is_a_glitch() -> None:
    assert regressed_reels({"a": {"views": 0}}, {"a": 290_418}) == {"a"}


def test_small_trim_and_growth_are_fine() -> None:
    insights = {"a": {"views": 999_950}, "b": {"views": 12}, "c": {"views": 5}}
    assert regressed_reels(insights, {"a": 1_000_000, "b": 10, "c": 40}) == set()


def test_new_reels_and_missing_views_are_ignored() -> None:
    assert regressed_reels({"new": {"views": 0}, "x": {"reach": 3}}, {"x": 500}) == set()
