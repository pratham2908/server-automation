"""Per-reel timelines, view speed, follow rate and the content breakdowns built from them."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import instagram_follow_sync as follow_sync
from app.services.instagram_account_insights import AccountDay
from app.services.instagram_content_analysis import (
    ReelSummary,
    content_groups,
    day_status,
    flatten_params,
    parse_published_at,
    per_1k,
    posting_slots,
    view_speed,
    views_at,
    watch_through,
)
from app.services.instagram_follow_attribution import ReelDay, ViewReading, daily_attribution, estimate_follows
from app.services.reel_duration import parse_ffprobe_duration
from app.timezone import IST

PUBLISHED = datetime(2026, 10, 1, 18, 0, tzinfo=IST)


def _at(hours: float, views: float) -> ViewReading:
    return ViewReading(at=PUBLISHED + timedelta(hours=hours), views=views)


def _day(date_: str, follows: float | None, reel_views: float = 1000) -> AccountDay:
    return AccountDay(
        date=date_,
        profile_visits=10,
        reel_views=reel_views,
        total_views=reel_views,
        follows=follows,
        unfollows=0,
        interactions=None,
        accounts_engaged=None,
        bio_link_taps=4,
    )


# ---- timeline ----


def test_timeline_lists_every_day_with_views_and_leaves_unreported_days_empty():
    start = datetime(2026, 10, 10, 0, 0, tzinfo=IST)
    readings = {
        "a": [
            ViewReading(start, 0),
            ViewReading(start + timedelta(hours=24), 600),
            ViewReading(start + timedelta(hours=48), 900),
        ],
        "b": [
            ViewReading(start, 0),
            ViewReading(start + timedelta(hours=24), 400),
            ViewReading(start + timedelta(hours=48), 500),
        ],
    }
    timelines = daily_attribution([_day("2026-10-10", 50)], readings)
    first, second = timelines["a"]
    assert (first.date, first.views, first.follows, first.bio_link_taps) == ("2026-10-10", 600, 30, 2.4)
    # Oct 11 has views but Instagram hasn't reported its follows: None, not 0.
    assert (second.date, second.views, second.follows, second.rough) == ("2026-10-11", 300, None, False)
    assert timelines["b"][0].follows == 20


def test_estimate_totals_match_the_timeline():
    start = datetime(2026, 10, 10, 0, 0, tzinfo=IST)
    readings = {"a": [ViewReading(start, 0), ViewReading(start + timedelta(hours=48), 1000)]}
    days = [_day("2026-10-10", 10, 500), _day("2026-10-11", 20, 500)]
    est = estimate_follows(days, readings)["a"]
    assert est.follows == pytest.approx(30)
    assert est.through == "2026-10-11"
    # A 48h gap between readings: each day is a guess, so the estimate is rough.
    assert est.quality == "rough"


def test_day_status_separates_waiting_from_never():
    today = date(2026, 10, 16)
    assert day_status("2026-10-12", True, today) == "estimated"
    assert day_status("2026-10-14", False, today) == "pending"
    assert day_status("2026-10-16", False, today) == "pending"
    assert day_status("2026-10-13", False, today) == "unavailable"


# ---- view speed ----


def test_views_at_interpolates_only_across_short_gaps():
    readings = [_at(0, 0), _at(20, 800), _at(28, 1000)]
    assert views_at(readings, PUBLISHED + timedelta(hours=24), 12) == pytest.approx(900)
    assert views_at(readings, PUBLISHED + timedelta(hours=10), 12) is None
    assert views_at(readings, PUBLISHED + timedelta(hours=40), 12) is None


def test_reel_tracked_from_the_start_gets_milestones_and_crossing_times():
    readings = [_at(6, 400), _at(18, 800), _at(30, 900), _at(54, 950), _at(160, 990), _at(172, 1000)]
    speed = view_speed(PUBLISHED, readings)
    assert speed.views_24h == pytest.approx(850)
    assert speed.views_48h == pytest.approx(937.5)
    assert speed.views_7d == pytest.approx(996.67, abs=0.01)
    # Half of 1000 views came in the 6h–18h stretch.
    assert speed.hours_to_half == pytest.approx(9)
    assert speed.hours_to_90 == pytest.approx(30)
    # No readings between 54h and 160h: whether it sped up lately can't be told.
    assert speed.picking_up is None


def test_reel_published_before_tracking_has_no_measured_first_day():
    speed = view_speed(PUBLISHED, [_at(240, 5000), _at(246, 5010)])
    assert (speed.views_24h, speed.views_48h, speed.views_7d) == (None, None, None)
    assert speed.hours_to_half is None


def test_young_reel_has_no_week_figures_yet():
    speed = view_speed(PUBLISHED, [_at(6, 100), _at(18, 200), _at(30, 300)])
    assert speed.views_24h is not None
    assert (speed.views_7d, speed.hours_to_half, speed.picking_up) == (None, None, None)


def test_an_old_reel_that_speeds_up_is_flagged_picking_up():
    # Flat at ~10 views a day, then 300 views in the last two days.
    readings = [_at(h, 2000 + (h - 200) * 10 / 24) for h in range(200, 284, 12)]
    readings += [_at(296, 2200), _at(308, 2350), _at(320, 2500)]
    assert view_speed(PUBLISHED, readings).picking_up is True


def test_published_at_reads_strings_and_naive_utc():
    assert parse_published_at("2026-10-01T12:30:00+00:00") == PUBLISHED
    assert parse_published_at(datetime(2026, 10, 1, 12, 30)) == PUBLISHED
    assert parse_published_at("not a date") is None


# ---- follow rate / hook ----


def test_every_reel_live_on_a_day_gets_the_same_follow_rate():
    # Why there is no per-reel follows-per-1k: the estimate shares a day's follows
    # by views, so the rate is the day's, not the reel's.
    start = datetime(2026, 10, 10, 0, 0, tzinfo=IST)
    readings = {
        "viral": [ViewReading(start, 0), ViewReading(start + timedelta(hours=24), 9000)],
        "flop": [ViewReading(start, 0), ViewReading(start + timedelta(hours=24), 100)],
    }
    timelines = daily_attribution([_day("2026-10-10", 91, 9100)], readings)
    rates = {reel: t[0].follows / t[0].views for reel, t in timelines.items()}
    assert rates["viral"] == pytest.approx(rates["flop"])


def test_per_1k_needs_enough_views():
    assert per_1k(3, 1000) == pytest.approx(3)
    assert per_1k(1, 50) is None
    assert per_1k(None, 1000) is None


def test_watch_through_needs_a_length():
    assert watch_through(8, 20) == pytest.approx(0.4)
    assert watch_through(8, None) is None
    assert watch_through(None, 20) is None


def test_ffprobe_duration_parsing():
    assert parse_ffprobe_duration('{"format": {"duration": "21.533"}}') == pytest.approx(21.533)
    assert parse_ffprobe_duration('{"format": {}}') is None
    assert parse_ffprobe_duration("garbage") is None


# ---- breakdowns ----


def _summary(media_id: str, published: datetime, **kw: Any) -> ReelSummary:
    base: dict[str, Any] = {
        "category": "DSA",
        "views": 1000.0,
        "skip_rate": 50.0,
        "watch_through": 0.5,
        "shares_per_1k": 2.0,
        "saves_per_1k": 1.0,
        "views_24h": None,
    }
    base.update(kw)
    return ReelSummary(media_id=media_id, published_at=published, **base)


def test_posting_slots_use_ist_and_only_mature_reels_for_lifetime_views():
    now = datetime(2026, 10, 16, 12, 0, tzinfo=IST)
    reels = [
        _summary("a", datetime(2026, 10, 1, 13, 0, tzinfo=IST), views_24h=800.0),  # Thursday 13:00
        _summary("b", datetime(2026, 10, 8, 13, 30, tzinfo=IST), views=3000.0),  # Thursday 13:30
        _summary("c", datetime(2026, 10, 15, 13, 10, tzinfo=IST), views=50.0, views_24h=400.0),  # too new
    ]
    hours, weekdays = posting_slots(reels, now)
    [one_pm] = hours
    assert (one_pm.slot, one_pm.reels, one_pm.measured, one_pm.median_views_24h) == (13, 3, 2, 600)
    assert (one_pm.mature, one_pm.median_views) == (2, 2000)
    # 1, 8 and 15 October 2026 are all Thursdays.
    assert [(w.slot, w.reels) for w in weekdays] == [(3, 3)]


def test_groups_split_by_category_and_each_param_value():
    t = datetime(2026, 10, 1, tzinfo=IST)
    reels = [
        _summary("a", t, shares_per_1k=6.0),
        _summary("b", t, shares_per_1k=2.0, category=None, skip_rate=70.0),
    ]
    params = {"a": {"format": ["comparison"]}, "b": {"format": ["walkthrough", "comparison"]}}
    groups = {(g.dimension, g.value): g for g in content_groups(reels, params)}
    assert groups[("category", "Uncategorized")].reels == 1
    comparison = groups[("format", "comparison")]
    assert comparison.reels == 2
    assert comparison.median_shares_per_1k == pytest.approx(4)
    assert comparison.median_skip_rate == 60
    assert groups[("format", "walkthrough")].median_shares_per_1k == 2


def test_flatten_params_keeps_plain_values():
    assert flatten_params({"format": "comparison", "items": ["a", 2], "nested": {"x": 1}, "blank": ""}) == {
        "format": ["comparison"],
        "items": ["a", "2"],
    }
    assert flatten_params(None) == {}


# ---- sync shell ----


class _Coll:
    def __init__(self, docs: list[dict[str, Any]] | None = None):
        self.docs = docs or []
        self.updates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.bulk: list[Any] = []
        self.unsets: list[dict[str, Any]] = []

    def find(self, query: dict[str, Any], projection: Any = None) -> Any:
        async def rows() -> Any:
            for doc in self.docs:
                yield doc

        return rows()

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> None:
        self.updates.append((query, update["$set"]))
        if "$unset" in update:
            self.unsets.append(update["$unset"])

    async def bulk_write(self, ops: list[Any], ordered: bool = True) -> None:
        self.bulk.extend(ops)


@pytest.mark.asyncio
async def test_write_estimates_stores_speed_and_daily_rows():
    published = datetime(2026, 10, 10, 0, 0, tzinfo=IST)
    db = SimpleNamespace(
        instagram_account_daily=_Coll([{"date": "2026-10-10", "follows": 10, "reel_views": 1000, "total_views": 1000}]),
        videos=_Coll([{"instagram_media_id": "m1", "published_at": published}]),
        instagram_reel_snapshots=_Coll(
            [
                {"media_id": "m1", "at": published + timedelta(hours=12), "views": 600},
                {"media_id": "m1", "at": published + timedelta(hours=24), "views": 1000},
                {"media_id": "m1", "at": published + timedelta(hours=36), "views": 1100},
            ]
        ),
        instagram_reel_daily=_Coll(),
    )
    await follow_sync._write_estimates(db, "c")
    [(query, stored)] = db.videos.updates
    assert query == {"channel_id": "c", "instagram_media_id": "m1"}
    assert stored["metadata.views_24h"] == 1000
    # A day's follows are never shared out per reel; exact ones are pasted in from the Business Suite table.
    assert "metadata.estimated_follows" not in stored
    assert db.videos.unsets == [{"metadata.estimated_follows": ""}]
    assert "metadata.follows_per_1k_views" not in stored
    rows = [op._doc for op in db.instagram_reel_daily.bulk]
    assert [(r["date"], r["views"], "follows" in r) for r in rows] == [
        ("2026-10-10", 1000, False),
        ("2026-10-11", 100, False),
    ]
