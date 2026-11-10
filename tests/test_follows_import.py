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


# ---- The CSV export ----------------------------------------------------------------------------------

import asyncio  # noqa: E402

from app.services.follows_import import CSV_SOURCE, apply_csv_rows, parse_csv  # noqa: E402
from tests.mongo_fakes import DB  # noqa: E402

HEADER = (
    '\ufeff"Post ID","Account ID","Account username","Account name",Description,"Duration (sec)","Publish time",'
    'Permalink,"Post type","Data comment",Date,Views,Reach,Likes,Shares,Follows,Comments,Saves\n'
)
CSV_TEXT = HEADER + (
    '18107269919377036,17841434303569138,themapworld,"The Map World","Where should 4 friends meet?\n'
    '#maps",34,10/30/2026 22:17,https://www.instagram.com/reel/A/,"IG reel",,Lifetime,41241,25501,900,23,9,40,12\n'
    '18123351959317071,17841418776716682,tryalgoviz,"TryAlgoViz",x,20,10/30/2026 07:56,p,"IG reel",,Lifetime,'
    '"62,979",38492,1,4,"1,206",2,3\n'
    '18000000000000001,17841418776716682,tryalgoviz,"TryAlgoViz",x,20,10/30/2026 07:56,p,"IG reel",,Lifetime,5,5,0,0,,0,0\n'
    '18000000000000002,17841418776716682,tryalgoviz,"TryAlgoViz",x,20,10/30/2026 07:56,p,"IG reel",,10/31/2026,5,5,0,0,3,0,0\n'
    '18999999999999999,17841434303569138,themapworld,"The Map World",x,20,11/09/2026 07:56,p,"IG reel",,Lifetime,9,9,0,0,2,0,0\n'
)


def test_csv_rows_carry_media_id_and_follows():
    rows = {r.media_id: r for r in parse_csv(CSV_TEXT)}
    assert rows["18107269919377036"].follows == 9 and rows["18107269919377036"].username == "themapworld"
    assert rows["18123351959317071"].follows == 1206 and rows["18123351959317071"].views == 62979
    # No follows number: skipped, not read as zero. A per-day row isn't a lifetime total.
    assert "18000000000000001" not in rows and "18000000000000002" not in rows


def test_a_post_listed_twice_keeps_the_higher_lifetime_count():
    again = CSV_TEXT + '18107269919377036,1,themapworld,x,x,1,10/30/2026 22:17,p,"IG reel",,Lifetime,1,1,0,0,4,0,0\n'
    assert {r.media_id: r.follows for r in parse_csv(again)}["18107269919377036"] == 9


def test_csv_follows_land_on_each_accounts_own_videos():
    db = DB(
        channels=[
            {"channel_id": "DreamScenicAi", "instagram_username": "themapworld"},
            {"channel_id": "TryAlgoViz", "instagram_username": "TryAlgoViz"},
        ],
        videos=[
            {"channel_id": "DreamScenicAi", "instagram_media_id": "18107269919377036", "metadata": {"views": 1}},
            {"channel_id": "TryAlgoViz", "instagram_media_id": "18123351959317071", "metadata": {}},
        ],
    )
    result = asyncio.run(apply_csv_rows(db, parse_csv(CSV_TEXT), date(2026, 11, 10)))
    assert result.matched == {"themapworld": 1, "tryalgoviz": 1}
    assert result.not_ours == {"themapworld": ["18999999999999999"]}
    meta = {v["instagram_media_id"]: v["metadata"] for v in db.videos.docs}
    assert meta["18107269919377036"] == {
        "views": 1,
        "follows": 9,
        "follows_source": CSV_SOURCE,
        "follows_as_of": "2026-11-10",
    }
    assert meta["18123351959317071"]["follows"] == 1206
