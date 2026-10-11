"""Reel watch time and skip rate: units, absence, and the fetch's fallback."""

from __future__ import annotations

import requests

from app.services.instagram import InstagramService
from app.services.instagram_reel_metrics import REEL_INSIGHT_METRICS, reel_metadata

# Numbers a real reel returned during the live probe.
LIVE = {
    "views": 134.0,
    "reach": 111.0,
    "saved": 1.0,
    "shares": 0.0,
    "total_interactions": 2.0,
    "ig_reels_avg_watch_time": 8539.0,
    "ig_reels_video_view_total_time": 990559.0,
    "reels_skip_rate": 40.0,
}


def test_the_watch_metrics_are_asked_for():
    assert {"ig_reels_avg_watch_time", "ig_reels_video_view_total_time", "reels_skip_rate", "views"} <= set(
        REEL_INSIGHT_METRICS
    )


def test_milliseconds_become_seconds_and_the_skip_rate_stays_a_percentage():
    meta = reel_metadata(1, 0, LIVE)
    assert meta["avg_view_duration_seconds"] == 8.54
    assert meta["total_watch_time_seconds"] == 990.6
    assert meta["estimated_hours_watched"] == 0.275
    assert meta["skip_rate"] == 40.0


def test_views_are_the_real_play_count_not_reach():
    assert reel_metadata(0, 0, LIVE)["views"] == 134
    assert reel_metadata(0, 0, LIVE)["reach"] == 111


def test_views_fall_back_to_reach_only_when_instagram_did_not_report_them():
    without_views = {k: v for k, v in LIVE.items() if k != "views"}
    assert reel_metadata(0, 0, without_views)["views"] == 111


def test_a_fractional_skip_rate_is_not_truncated():
    assert reel_metadata(0, 0, {**LIVE, "reels_skip_rate": 50.7})["skip_rate"] == 50.7


def test_a_metric_instagram_did_not_return_is_absent_not_zero():
    """A 0 would overwrite a real earlier value on the next sync."""
    meta = reel_metadata(3, 1, {"reach": 10.0, "views": 12.0})
    assert "skip_rate" not in meta and "avg_view_duration_seconds" not in meta
    assert "total_watch_time_seconds" not in meta
    assert meta["likes"] == 3 and meta["comments"] == 1


def test_missing_like_and_comment_counts_are_zero():
    meta = reel_metadata(None, None, {})
    assert meta["likes"] == 0 and meta["comments"] == 0


def test_a_failed_insights_call_leaves_the_stored_counts_alone():
    """Every insight failing for a reel must not write reach/views/saves/shares as 0."""
    meta = reel_metadata(4, 2, {})
    assert meta == {"likes": 4, "comments": 2}


# ---------- the fetch ----------


def _body(**values: float) -> dict:
    return {"data": [{"name": k, "values": [{"value": v}]} for k, v in values.items()]}


def _svc(get):
    svc = InstagramService(access_token="t", provider="facebook")
    svc._get = get  # type: ignore[method-assign]
    return svc


def test_a_reel_is_fetched_with_one_request_and_keeps_float_values():
    asked = []

    def get(endpoint, params=None):
        asked.append(params["metric"])
        return _body(reels_skip_rate=50.7, views=486)

    out = _svc(get).get_reel_insights(["m1"])
    assert out == {"m1": {"reels_skip_rate": 50.7, "views": 486.0}}
    assert len(asked) == 1 and "reels_skip_rate" in asked[0]


def test_a_refused_batch_falls_back_to_per_metric_and_keeps_what_exists():
    """An old reel with no skip rate must not lose its views and reach too."""
    asked = []

    def get(endpoint, params=None):
        asked.append(params["metric"])
        if "," in params["metric"] or params["metric"] == "reels_skip_rate":
            raise requests.HTTPError("metric not supported for this media")
        return _body(**{params["metric"]: 7})

    out = _svc(get).get_reel_insights(["old"])
    assert "reels_skip_rate" not in out["old"]
    assert out["old"]["views"] == 7.0 and out["old"]["ig_reels_avg_watch_time"] == 7.0
    assert len(asked) == 1 + len(REEL_INSIGHT_METRICS)  # the batch, then each metric once


def test_one_unreadable_reel_does_not_stop_the_others():
    def get(endpoint, params=None):
        if endpoint.startswith("bad"):
            raise RuntimeError("network")
        return _body(views=5)

    out = _svc(get).get_reel_insights(["bad/x", "good"])
    assert "bad/x" not in out and out["good"] == {"views": 5.0}


def test_engagement_rate_and_replay_ratio_are_derived_from_reach():
    meta = reel_metadata(1, 0, LIVE)

    assert meta["total_interactions"] == 2
    assert meta["engagement_rate"] == 1.8  # 2 / 111 reached accounts
    assert meta["views_per_reach"] == 1.21  # 134 plays / 111 accounts


def test_no_reach_means_no_division():
    meta = reel_metadata(0, 0, {"views": 5.0})

    assert "engagement_rate" not in meta
    assert "views_per_reach" not in meta
