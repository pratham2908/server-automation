"""Reel insights read off a screen recording of the Instagram app (OCR frames from a real 11-17 recording)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.services import app_insights as ai
from app.services import app_insights_store as store
from app.timezone import IST
from tests.mongo_fakes import DB

OCR = (Path(__file__).parent / "fixtures" / "app_insights_ocr.txt").read_text()
AT = datetime(2026, 11, 17, 15, 45, 35, tzinfo=IST)


@pytest.fixture(scope="module")
def reels() -> list[ai.ReelInsights]:
    return ai.read_reels(ai.parse_ocr(OCR))


def test_counts_read_as_the_app_prints_them():
    assert ai.parse_count("1,129,593") == 1129593
    assert ai.parse_count("7K") == 7000
    assert ai.parse_count("1.1 M") == 1100000
    assert ai.parse_count("Follows") is None


def test_one_record_per_reel_between_feed_frames(reels):
    assert [r.counts.get("follows") for r in reels] == [644, 15, 21]


def test_overview_engagement_and_audience(reels):
    tokyo = reels[0]
    assert tokyo.counts == {
        "likes": 17900,
        "comments": 131,
        "reposts": 304,
        "shares": 4600,
        "saves": 2900,
        "profile_visits": 7000,
        "bio_link_clicks": 63,
        "follows": 644,
        "views": 1129593,
        "viewers": 593272,
    }
    assert tokyo.shown["profile_visits"] == "7K"  # rounded by the app, and kept as shown
    assert tokyo.avg_watch_seconds == 14
    assert (tokyo.followers_pct, tokyo.non_followers_pct) == (0.2, 99.8)
    assert tokyo.country_pct == {"Indonesia": 10.8, "United States": 10.8}


def test_full_age_split_in_order(reels):
    assert reels[2].age_pct == {
        "13-17": 13.9,
        "18-24": 42.3,
        "25-34": 35.9,
        "35-44": 4.9,
        "45-54": 1.9,
        "55-64": 0.5,
        "65+": 0.7,
    }
    assert list(reels[2].country_pct)[:2] == ["United States", "United Kingdom"]


def test_matched_by_views_or_by_city_and_likes(reels):
    stored = [
        {"media_id": "tokyo", "title": "Where Should 4 Friends Meet? #7: Tokyo", "views": 1129590, "likes": 17933},
        {"media_id": "tokyo2", "title": "Tokyo: Shibuya to Skytree", "views": 44347, "likes": 183},
        {"media_id": "miami", "title": "Where Should 4 Friends Meet? #32: Miami", "views": 157631, "likes": 1040},
        {"media_id": "miami-old", "title": "Miami: Beach to Airport", "views": 5000, "likes": 40},
    ]
    assert ai.match_reel(reels[0], stored) == "tokyo"
    assert ai.match_reel(reels[1], stored) is None  # Berlin isn't stored: no guess
    assert ai.match_reel(reels[2], stored) == "miami"  # no view count on screen: city + likes


def test_similar_reach_reel_settled_by_city(reels):
    """Stats refreshed after the recording: two reels within tolerance, the title's city decides."""
    stored = [
        {"media_id": "tokyo", "title": "Where Should 4 Friends Meet? #7: Tokyo", "views": 1131000, "likes": 17990},
        {"media_id": "kl", "title": "Where Should 4 Friends Meet? #21: Kuala Lumpur", "views": 1120000, "likes": 9000},
    ]
    berlin = ai.ReelInsights(thumbnail_text=["4 FRIENDS MEET", "BERLIN"], counts={"views": 53705})
    near_berlin = [
        {"media_id": "berlin", "title": "Where Should 4 Friends Meet? #33: Berlin", "views": 53921},
        {"media_id": "other", "title": "Where Should 4 Friends Meet? #3: Oslo", "views": 54000},
    ]
    near_berlin[0]["likes"] = near_berlin[1]["likes"] = 280
    berlin.counts["likes"] = 272
    assert ai.match_reel(berlin, near_berlin) == "berlin"  # the series name in every title doesn't decide it
    assert ai.match_reel(reels[0], stored) == "tokyo"
    stored[1] |= {"title": "Tokyo again", "likes": 17950}  # both name the city, likes agree: no guess
    assert ai.match_reel(reels[0], stored) is None


def _db(follows: int | None) -> DB:
    return DB(
        videos=[
            {
                "channel_id": "DreamScenicAi",
                "instagram_media_id": "tokyo",
                "title": "Where Should 4 Friends Meet? #7: Tokyo",
                "metadata": {"views": 1129590, "likes": 17933, "comments": 131, "follows": follows},
            }
        ]
    )


def test_stored_with_history_and_follows_only_rise(reels):
    db = _db(follows=638)
    result = asyncio.run(store.store(db, "DreamScenicAi", reels, AT, "rec.mp4"))
    assert [m for m, _ in result.stored] == ["tokyo"] and len(result.unmatched) == 2
    meta = db.videos.docs[0]["metadata"]
    assert meta["follows"] == 644 and meta["follows_source"] == store.FOLLOWS_SOURCE
    assert meta["app_insights"]["counts"]["bio_link_clicks"] == 63
    # An older recording imported afterwards adds history but doesn't lower follows or replace the latest.
    older = ai.ReelInsights(counts={"views": 1129000, "likes": 17900, "comments": 130, "follows": 600})
    asyncio.run(store.store(db, "DreamScenicAi", [older], AT - timedelta(days=1), "old.mp4"))
    assert len(db.reel_app_insights.docs) == 2
    meta = db.videos.docs[0]["metadata"]
    assert meta["follows"] == 644 and meta["app_insights"]["source"] == "rec.mp4"


def test_sources_of_views_kept_apart_from_countries():
    """Istanbul from the 16:24 recording: sources and countries share a layout; Türkiye has an accent."""
    (istanbul,) = ai.read_reels(
        ai.parse_ocr((Path(__file__).parent / "fixtures" / "app_insights_ocr_sources.txt").read_text())
    )
    assert istanbul.source_pct == {"Reels tab": 74.2, "Explore": 12.4, "Feed": 7.0, "Profile": 3.3, "Stories": 0.1}
    assert list(istanbul.country_pct) == ["Türkiye", "Germany", "United States", "United Kingdom", "Italy"]
    assert istanbul.country_pct["Türkiye"] == 75.0
    assert istanbul.as_doc()["source_pct"]["Reels tab"] == 74.2


def test_cyrillic_lookalikes_read_as_counts():
    ((_, tokens),) = ai.parse_ocr("### f\n0.90,0.34|3К\n")
    assert ai.parse_count(tokens[0][2]) == 3000


def test_recording_time_from_either_name():
    from scripts.import_app_insights import recorded_at

    assert recorded_at(Path("ScreenRecording_11-17-2026 16-24-51_1.MP4")) == datetime(
        2026, 11, 17, 16, 24, 51, tzinfo=IST
    )
    assert recorded_at(Path("city_solved-2026-11-17-16-24.mp4")) == datetime(2026, 11, 17, 16, 24, tzinfo=IST)


def test_new_reel_grown_past_the_tolerance_still_found():
    """London #31: 91,635 views when recorded, 93,933 (+2.5%) by the time it was imported."""
    london = ai.ReelInsights(thumbnail_text=["LONDON", "S MEET"], counts={"views": 91635, "likes": 351})
    stored = [
        {"media_id": "london31", "title": "Where Should 4 Friends Meet? #31: London", "views": 93933, "likes": 360},
        {"media_id": "wembley", "title": "London: Wembley Stadium to Greenwich, 4 Ways", "views": 99000, "likes": 400},
    ]
    assert ai.match_reel(london, stored) == "london31"
    stored[0]["likes"] = 900  # likes disagree: not this reel, and no guess
    assert ai.match_reel(london, stored) is None


def test_rounded_likes_allow_for_the_rounding():
    """Miami shows "1K" likes; we hold 1,082, beyond 6% of 1,000 but inside the rounding."""
    miami = ai.ReelInsights(thumbnail_text=["MIAMI"], counts={"views": 157674, "likes": 1000}, shown={"likes": "1K"})
    stored = [
        {"media_id": "miami", "title": "Where Should 4 Friends Meet? #32: Miami", "views": 168000, "likes": 1082},
        {"media_id": "other", "title": "Where Should 4 Friends Meet? #30: Oslo", "views": 165000, "likes": 1050},
    ]
    assert ai.match_reel(miami, stored) == "miami"


def test_scrolled_to_sources_stays_the_same_reel():
    """Miami in the 16:20 recording: the title scrolled away for a moment, then only sources showed."""
    text = (Path(__file__).parent / "fixtures" / "app_insights_ocr_split.txt").read_text()
    (miami,) = ai.read_reels(ai.parse_ocr(text))
    assert miami.counts["views"] == 159390
    assert miami.source_pct["Reels tab"] == 84.8


def test_latest_view_takes_each_field_from_the_newest_recording_that_showed_it():
    first, second = AT, AT + timedelta(minutes=35)
    rows = [
        {
            "captured_at": first,
            "source": "a.mp4",
            "counts": {"views": 100, "profile_visits": 7000},
            "shown": {"views": "100", "profile_visits": "7K"},
            "country_pct": {"Japan": 60.0},
            "source_pct": {},
        },
        {
            "captured_at": second,
            "source": "b.mp4",
            "counts": {"views": 120},
            "shown": {"views": "120"},
            "country_pct": {},
            "source_pct": {"Reels tab": 75.0},
        },
    ]
    view = ai.latest_view(rows)
    assert view["counts"] == {"views": 120, "profile_visits": 7000}
    assert view["shown"]["profile_visits"] == "7K" and view["as_of"]["profile_visits"] == first
    assert view["country_pct"] == {"Japan": 60.0} and view["source_pct"] == {"Reels tab": 75.0}
    assert view["captured_at"] == second and view["source"] == "b.mp4"


def test_a_reel_of_similar_reach_isnt_taken_for_one_that_grew_away():
    """Miami #32 recorded at 157,674 had grown past 2%; a San Francisco reel sat at 160,236."""
    miami = ai.ReelInsights(
        thumbnail_text=["MIAMI", "4 FRIENDS MEET"], counts={"views": 157674, "likes": 1000}, shown={"likes": "1K"}
    )
    stored = [
        {
            "media_id": "sf",
            "title": "San Francisco: Why Doesn't Google Maps Take the Shortest Road?",
            "views": 160236,
            "likes": 594,
        },
        {"media_id": "miami", "title": "Where Should 4 Friends Meet? #32: Miami", "views": 161500, "likes": 1090},
    ]
    assert ai.match_reel(miami, stored) == "miami"
    stored[0]["likes"] = 1000  # even with likes alike, the title has to name the city
    assert ai.match_reel(miami, stored[:1]) is None


def test_due_for_recording_only_movers_and_big_unrecorded_reels():
    reels = [
        {"media_id": "settled", "title": "Tokyo", "views": 1150000, "recorded_views": 1129593, "recorded_at": AT},
        {"media_id": "moving", "title": "Berlin #33", "views": 80000, "recorded_views": 54249, "recorded_at": AT},
        {"media_id": "faster", "title": "Miami", "views": 320000, "recorded_views": 159390, "recorded_at": AT},
        {"media_id": "big-new", "title": "Zombie", "views": 450000, "recorded_views": None, "recorded_at": None},
        {"media_id": "small-new", "title": "Pathfinder", "views": 40000, "recorded_views": None, "recorded_at": None},
        {"media_id": "no-overview", "title": "BA", "views": 270000, "recorded_views": None, "recorded_at": AT},
        {"media_id": "small-glimpse", "title": "Wembley", "views": 52898, "recorded_views": None, "recorded_at": AT},
    ]
    due = ai.due_for_recording(reels)
    assert [d.media_id for d in due] == ["faster", "moving", "no-overview", "big-new"]
    assert round(due[0].growth or 0, 2) == 1.01


def test_due_reels_reads_the_view_counts_own_date():
    db = _db(follows=None)
    asyncio.run(
        store.store(
            db,
            "DreamScenicAi",
            [ai.ReelInsights(counts={"views": 1129590, "likes": 17933, "comments": 131})],
            AT,
            "rec.mp4",
        )
    )
    db.videos.docs[0]["metadata"]["views"] = 1500000
    (tokyo,) = asyncio.run(store.due_reels(db, "DreamScenicAi"))
    assert tokyo.views_recorded == 1129590 and tokyo.recorded_at == AT


def _zara(views: int, likes: int, comments: int) -> ai.ReelInsights:
    return ai.ReelInsights(thumbnail_text=["SVITVI"], counts={"views": views, "likes": likes, "comments": comments})


def test_photo_thumbnail_matched_by_views_likes_and_comments():
    """Zara's reels: no words on the thumbnail. Two reels of ~5.4K views and 119 likes differ in comments."""
    stored = [
        {"media_id": "a", "title": "Shaadi mein kaun si best?", "views": 5600, "likes": 121, "comments": 56},
        {"media_id": "b", "title": "Family secret", "views": 5450, "likes": 120, "comments": 43},
    ]
    assert ai.match_reel(_zara(5529, 119, 55), stored) == "a"
    assert ai.match_reel(_zara(5383, 119, 43), stored) == "b"
    assert ai.match_reel(_zara(5383, 119, 20), stored) is None  # comments agree with neither


def test_carousel_matched_by_likes_and_comments_alone():
    posts = [
        {"media_id": "nov4", "likes": 45, "comments": 26},
        {"media_id": "nov11", "likes": 46, "comments": 37},
    ]
    post = ai.ReelInsights(kind="post", counts={"likes": 45, "comments": 26, "views": 858})
    assert ai.match_post(post, posts) == "nov4"
    assert ai.match_post(ai.ReelInsights(kind="post", counts={"likes": 45}), posts) is None


def test_post_insights_read_as_a_carousel():
    """A carousel's Overview from the Zara 18:13 recording: profile visits sit in a tile."""
    text = "\n".join(
        [
            "### f_0171.jpg",
            "0.37,0.08|Post insights",
            *(f"{x},0.40|{v}" for x, v in ((0.12, 45), (0.30, 26), (0.48, 3), (0.65, 4), (0.83, 4))),
            "0.08,0.45|Overview",
            "0.39,0.45|Engagement",
            "0.75,0.45|Audience",
            "0.08,0.58|Views",
            "0.55,0.58|Viewers",
            "0.08,0.61|858",
            "0.56,0.61|424",
            "0.08,0.68|Profile visits",
            "0.55,0.69|Follows",
            "0.08,0.72|5",
        ]
    )
    (post,) = ai.read_reels(ai.parse_ocr(text))
    assert post.kind == "post" and post.as_doc()["kind"] == "post"
    assert post.counts["likes"] == 45 and post.counts["comments"] == 26
    assert post.counts["views"] == 858 and post.counts["profile_visits"] == 5


def test_carousel_stored_on_our_record_found_by_caption():
    db = DB(
        videos=[],
        posts=[
            {
                "channel_id": "zara kapoor",
                "post_id": "p1",
                "kind": "carousel",
                "status": "published",
                "caption": "Is dress ne mujhe nahi,  maine isse choose kiya",
            },
        ],
    )
    ig = [
        {"media_id": "nov4", "caption": "Is dress ne mujhe nahi, maine isse choose kiya", "likes": 45, "comments": 26},
        {"media_id": "old", "caption": "Pink mein dangerous", "likes": 26, "comments": 16},
    ]
    recs = [
        ai.ReelInsights(kind="post", counts={"likes": 45, "comments": 26, "views": 858}),
        ai.ReelInsights(kind="post", counts={"likes": 26, "comments": 16, "views": 700}),
    ]
    result = asyncio.run(store.store(db, "zara kapoor", recs, AT, "zara.mp4", posts=ig))
    assert [m for m, _ in result.stored] == ["nov4", "old"]
    assert "no server record" in result.stored[1][1]
    assert db.posts.docs[0]["app_insights"]["counts"]["views"] == 858
    assert {d["media_id"] for d in db.reel_app_insights.docs} == {"nov4", "old"}
