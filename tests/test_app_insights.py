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
    assert ai.match_reel(reels[0], stored) == "tokyo"
    stored[1]["title"] = "Tokyo again"  # both name the city: no guess
    assert ai.match_reel(reels[0], stored) is None


def _db(follows: int | None) -> DB:
    return DB(
        videos=[
            {
                "channel_id": "DreamScenicAi",
                "instagram_media_id": "tokyo",
                "title": "Where Should 4 Friends Meet? #7: Tokyo",
                "metadata": {"views": 1129590, "likes": 17933, "follows": follows},
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
    older = ai.ReelInsights(counts={"views": 1129000, "follows": 600})
    asyncio.run(store.store(db, "DreamScenicAi", [older], AT - timedelta(days=1), "old.mp4"))
    assert len(db.reel_app_insights.docs) == 2
    meta = db.videos.docs[0]["metadata"]
    assert meta["follows"] == 644 and meta["app_insights"]["source"] == "rec.mp4"
