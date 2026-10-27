"""Exact per-reel follows pasted from the Business Suite table."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.services.follows_import import FollowRow, match_rows, parse_paste
from app.timezone import IST

TODAY = date(2026, 10, 28)

PASTE = """
Title
Instagram
15 Must-Know Networking Concepts 15 Must-Know Networking Concepts Fifteen live tiles
Reel

tryalgoviz
11 October 10:02
876
211,732
165,530
‑‑
7,844
Instagram
Dijkstra caption
Reel

tryalgoviz
27 October 13:33
40
525,993
Instagram
Cut off reel
Reel

tryalgoviz
27 October 10:32
0
"""


def test_each_reel_block_gives_publish_time_and_follows() -> None:
    rows = parse_paste(PASTE, TODAY)
    assert rows == [
        FollowRow(datetime(2026, 10, 11, 10, 2, tzinfo=IST), 876),
        FollowRow(datetime(2026, 10, 27, 13, 33, tzinfo=IST), 40),
        FollowRow(datetime(2026, 10, 27, 10, 32, tzinfo=IST), 0),
    ]


def test_a_date_still_in_the_future_is_last_year() -> None:
    rows = parse_paste("Reel\nacct\n29 December 18:47\n3\n", date(2026, 1, 5))
    assert rows[0].published == datetime(2025, 12, 29, 18, 47, tzinfo=IST)


def test_a_block_that_does_not_fit_is_skipped_not_guessed() -> None:
    assert parse_paste("Reel\nacct\nnot a date\n5\nReel\nacct\n11 October 10:02\n‑‑\n", TODAY) == []


def test_rows_pair_with_the_reel_published_within_minutes() -> None:
    t = datetime(2026, 10, 11, 10, 2, tzinfo=IST)
    reels = {"a": t + timedelta(seconds=40), "b": t + timedelta(hours=3), "c": None}
    matched, unmatched = match_rows([FollowRow(t, 876), FollowRow(t + timedelta(days=1), 5)], reels)
    assert matched == {"a": 876}
    assert unmatched == [FollowRow(t + timedelta(days=1), 5)]


def test_two_reels_minutes_apart_do_not_share_a_row() -> None:
    t = datetime(2026, 10, 11, 10, 0, tzinfo=IST)
    reels = {"a": t, "b": t + timedelta(minutes=2)}
    matched, unmatched = match_rows([FollowRow(t + timedelta(minutes=1), 7)], reels)
    assert len(matched) == 1 and unmatched == []
