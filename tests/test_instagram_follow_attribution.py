"""Account-day parsing and the per-reel follow estimate built from it."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services.instagram_account_insights import AccountDay, parse_account_day
from app.services.instagram_follow_attribution import (
    ViewReading,
    estimate_follows,
    views_by_day,
)
from app.timezone import IST

# Bodies shaped like the live responses for TryAlgoViz on 2026-10-09.
TOTALS = {
    "data": [
        {"name": "profile_views", "total_value": {"value": 136}},
        {"name": "total_interactions", "total_value": {"value": 1559}},
        {"name": "accounts_engaged", "total_value": {"value": 1286}},
    ]
}
VIEWS = {
    "data": [
        {
            "name": "views",
            "total_value": {
                "value": 293566,
                "breakdowns": [
                    {
                        "dimension_keys": ["media_product_type"],
                        "results": [
                            {"dimension_values": ["STORY"], "value": 19},
                            {"dimension_values": ["REEL"], "value": 293547},
                        ],
                    }
                ],
            },
        }
    ]
}
FOLLOWS = {
    "data": [
        {
            "name": "follows_and_unfollows",
            "total_value": {
                "breakdowns": [
                    {
                        "dimension_keys": ["follow_type"],
                        "results": [
                            {"dimension_values": ["FOLLOWER"], "value": 78},
                            {"dimension_values": ["NON_FOLLOWER"], "value": 2},
                        ],
                    }
                ]
            },
        }
    ]
}
# What Instagram returns for a day it hasn't finished counting.
FOLLOWS_PENDING = {
    "data": [{"name": "follows_and_unfollows", "total_value": {"breakdowns": [{"dimension_keys": ["follow_type"]}]}}]
}


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=IST)


def account_day(date: str, follows: int | None, reel_views: int, total_views: int | None = None) -> AccountDay:
    return AccountDay(
        date=date,
        profile_visits=40,
        reel_views=reel_views,
        total_views=reel_views if total_views is None else total_views,
        follows=follows,
        unfollows=0,
        interactions=None,
        accounts_engaged=None,
    )


def test_a_reported_day_is_parsed_into_follows_unfollows_and_reel_views():
    day = parse_account_day("2026-10-09", TOTALS, VIEWS, FOLLOWS)
    assert day == AccountDay(
        date="2026-10-09",
        profile_visits=136,
        reel_views=293547,
        total_views=293566,
        follows=78,
        unfollows=2,
        interactions=1559,
        accounts_engaged=1286,
    )


def test_a_day_instagram_has_not_counted_yet_has_no_follows_rather_than_zero():
    day = parse_account_day("2026-10-10", TOTALS, VIEWS, FOLLOWS_PENDING)
    assert day.follows is None and day.unfollows is None


def test_a_failed_call_leaves_its_fields_unknown():
    day = parse_account_day("2026-10-09", None, None, None)
    assert day.profile_visits is None and day.reel_views is None and day.follows is None


def test_views_are_split_across_days_by_time():
    # 120 views between 18:00 on the 9th and 06:00 on the 10th: half each side of midnight.
    by_day = views_by_day([ViewReading(at(9, 18), 0), ViewReading(at(10, 6), 120)])
    assert by_day["2026-10-09"].views == pytest.approx(60)
    assert by_day["2026-10-10"].views == pytest.approx(60)
    assert by_day["2026-10-09"].span_hours == pytest.approx(12)


def test_readings_out_of_order_or_falling_are_handled():
    by_day = views_by_day([ViewReading(at(10, 12), 100), ViewReading(at(9, 12), 0), ViewReading(at(11, 0), 90)])
    # The drop from 100 to 90 adds nothing; the 0→100 gain still lands.
    assert sum(d.views for d in by_day.values()) == pytest.approx(100)


def test_follows_follow_each_reels_share_of_the_days_views():
    readings = {
        "breakout": [ViewReading(at(9, 0), 0), ViewReading(at(10, 0), 900)],
        "quiet": [ViewReading(at(9, 0), 0), ViewReading(at(10, 0), 100)],
    }
    est = estimate_follows([account_day("2026-10-09", follows=50, reel_views=1000)], readings)
    assert est["breakout"].follows == pytest.approx(45)
    assert est["quiet"].follows == pytest.approx(5)
    assert est["breakout"].profile_visits == pytest.approx(36)
    assert est["breakout"].quality == "good" and est["breakout"].through == "2026-10-09"


def test_views_from_reels_we_do_not_track_keep_their_share_of_follows():
    readings = {"tracked": [ViewReading(at(9, 0), 0), ViewReading(at(10, 0), 250)]}
    est = estimate_follows([account_day("2026-10-09", follows=40, reel_views=1000)], readings)
    assert est["tracked"].follows == pytest.approx(10)


def test_follows_from_stories_are_not_given_to_reels():
    readings = {"reel": [ViewReading(at(9, 0), 0), ViewReading(at(10, 0), 800)]}
    est = estimate_follows([account_day("2026-10-09", follows=10, reel_views=800, total_views=1000)], readings)
    assert est["reel"].follows == pytest.approx(8)


def test_days_without_reported_follows_are_skipped():
    readings = {"new": [ViewReading(at(10, 0), 0), ViewReading(at(11, 0), 500)]}
    assert estimate_follows([account_day("2026-10-10", follows=None, reel_views=500)], readings) == {}


def test_views_spread_over_a_long_gap_make_the_estimate_rough():
    readings = {"old": [ViewReading(at(1, 0), 0), ViewReading(at(9, 0), 800)]}
    est = estimate_follows([account_day("2026-10-05", follows=10, reel_views=100)], readings)
    assert est["old"].quality == "rough"


def test_a_day_built_from_far_apart_readings_is_shared_among_tracked_reels_only():
    # Readings 48h apart can't say how much of the account's 1000 views were these
    # reels', so the day's follows go to them rather than to "untracked".
    readings = {
        "a": [ViewReading(at(8, 0), 0), ViewReading(at(10, 0), 300)],
        "b": [ViewReading(at(8, 0), 0), ViewReading(at(10, 0), 100)],
    }
    est = estimate_follows([account_day("2026-10-09", follows=40, reel_views=1000)], readings)
    assert est["a"].follows == pytest.approx(30) and est["b"].follows == pytest.approx(10)


# ---- bio-link taps and reach (website_clicks, reach by follow_type) ----

TOTALS_WITH_TAPS = {"data": [*TOTALS["data"], {"name": "website_clicks", "total_value": {"value": 4}}]}
REACH = {
    "data": [
        {
            "name": "reach",
            "total_value": {
                "value": 33328,
                "breakdowns": [
                    {
                        "dimension_keys": ["follow_type"],
                        "results": [
                            {"dimension_values": ["FOLLOWER"], "value": 245},
                            {"dimension_values": ["NON_FOLLOWER"], "value": 33132},
                        ],
                    }
                ],
            },
        }
    ]
}


def test_bio_link_taps_and_reach_split_are_parsed():
    day = parse_account_day("2026-10-15", TOTALS_WITH_TAPS, VIEWS, FOLLOWS, REACH)
    assert day.bio_link_taps == 4
    assert (day.reach, day.reach_followers, day.reach_non_followers) == (33328, 245, 33132)


def test_missing_reach_call_and_older_totals_leave_the_new_fields_unknown():
    day = parse_account_day("2026-10-15", TOTALS, VIEWS, FOLLOWS)
    assert day.bio_link_taps is None and day.reach is None and day.reach_non_followers is None


def test_bio_link_taps_are_shared_out_like_profile_visits():
    day = AccountDay(
        date="2026-10-09",
        profile_visits=40,
        reel_views=100,
        total_views=100,
        follows=10,
        unfollows=0,
        interactions=None,
        accounts_engaged=None,
        bio_link_taps=8,
    )
    readings = {
        "a": [ViewReading(at(9, 0), 0), ViewReading(at(10, 0), 75)],
        "b": [ViewReading(at(9, 0), 0), ViewReading(at(10, 0), 25)],
    }
    est = estimate_follows([day], readings)
    assert est["a"].bio_link_taps == pytest.approx(6)
    assert est["b"].bio_link_taps == pytest.approx(2)
