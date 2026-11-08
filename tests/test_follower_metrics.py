"""The Followers page's period numbers: per day, the period's totals, and per reel."""

from __future__ import annotations

from datetime import date, datetime

from app.services import follower_metrics as fm


def _rows() -> list[dict]:
    days = fm.day_labels(date(2026, 11, 8), 4)  # 5, 6, 7, 8 Nov
    insights = {
        "2026-11-05": {"follows": 219, "unfollows": 2},
        "2026-11-06": {"follows": 154, "unfollows": 4},
    }
    counts = {"2026-11-04": 126, "2026-11-05": 328, "2026-11-06": 576, "2026-11-07": 973, "2026-11-08": 1659}
    return fm.series(days, insights, counts, {"2026-11-06": 10}), counts


def test_days_are_the_last_n_ending_today() -> None:
    assert fm.day_labels(date(2026, 11, 8), 3) == ["2026-11-06", "2026-11-07", "2026-11-08"]


def test_a_reported_day_has_no_estimate_and_a_pending_one_has_the_count_change() -> None:
    rows, _ = _rows()
    by_day = {r["date"]: r for r in rows}
    assert (by_day["2026-11-06"]["follows"], by_day["2026-11-06"]["net_estimate"]) == (154, None)
    assert by_day["2026-11-06"]["followed_still_following"] == 10
    assert (by_day["2026-11-07"]["follows"], by_day["2026-11-07"]["net_estimate"]) == (None, 397)
    assert by_day["2026-11-08"]["net_estimate"] == 686 and by_day["2026-11-08"]["followers"] == 1659


def test_no_estimate_without_both_counts() -> None:
    rows = fm.series(["2026-11-07"], {}, {"2026-11-07": 973}, {})
    assert rows[0]["net_estimate"] is None


def test_totals_sum_reported_days_and_keep_pending_apart() -> None:
    rows, counts = _rows()
    t = fm.totals(rows, counts)
    assert (t["follows"], t["unfollows"], t["net"], t["days_reported"]) == (373, 6, 367, 2)
    assert t["days_pending"] == ["2026-11-07", "2026-11-08"] and t["pending_net_estimate"] == 397 + 686
    assert t["best_day"] == {"date": "2026-11-05", "follows": 219}
    assert (t["followers_start"], t["followers_end"]) == (126, 1659)


def test_reels_are_most_follows_first_with_a_rate_per_1k_views() -> None:
    videos = [
        {"instagram_media_id": "a", "title": "Rome\nlong caption", "metadata": {"follows": None, "views": 900}},
        {"instagram_media_id": "b", "title": "Tokyo", "metadata": {"follows": 563, "views": 200_000}},
        {
            "instagram_media_id": "c",
            "title": "Paris",
            "published_at": datetime(2026, 11, 1),
            "metadata": {"follows": 9},
        },
    ]
    rows = fm.reel_rows(videos, lambda d: d and d.isoformat())
    assert [r["title"] for r in rows] == ["Tokyo", "Paris", "Rome"]
    assert rows[0]["follows_per_1k_views"] == 2.81 and rows[1]["follows_per_1k_views"] is None
    assert rows[1]["published_at"] == "2026-11-01T00:00:00"


def test_an_old_unreported_day_is_unavailable_not_pending() -> None:
    days = fm.day_labels(date(2026, 11, 8), 6)  # 3..8 Nov; 3 Nov never reported (under 100 followers)
    insights = {d: {"follows": 5, "unfollows": 0} for d in days[1:4]}
    t = fm.totals(fm.series(days, insights, {}, {}), {})
    assert t["days_unavailable"] == ["2026-11-03"]
    assert t["days_pending"] == ["2026-11-07", "2026-11-08"]
