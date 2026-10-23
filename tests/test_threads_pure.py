"""Threads post text, token timing, and insights parsing — the pure pieces."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.threads_insights_parse import account_daily_views, account_totals, demographics, post_insights
from app.services.threads_text import MAX_TEXT_CHARS, threads_text, topic_tag
from app.services.threads_token_policy import expires_at, should_warn, token_state

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


class TestText:
    def test_caption_is_the_post(self) -> None:
        assert threads_text({"description": "  Why Gibraltar is British  ", "title": "t"}) == "Why Gibraltar is British"

    def test_title_when_no_caption(self) -> None:
        assert threads_text({"description": "", "title": "Gibraltar"}) == "Gibraltar"

    def test_long_caption_is_cut_at_a_word_within_the_limit(self) -> None:
        text = threads_text({"description": "word " * 200})
        assert len(text) <= MAX_TEXT_CHARS
        assert text.endswith("…")
        assert not text[:-1].endswith("wor")

    def test_topic_is_the_first_tag_threads_accepts(self) -> None:
        assert topic_tag({"tags": ["#", "Geo.Politics & Maps", "other"]}) == "GeoPolitics Maps"
        assert topic_tag({"tags": []}) is None
        assert topic_tag({"tags": ["x" * 80]}) == "x" * 50


class TestTokenPolicy:
    def test_states(self) -> None:
        assert token_state(None, NOW) == "missing"
        assert token_state({"access_token": "t", "refreshed_at": NOW - timedelta(hours=3)}, NOW) == "fresh"
        assert token_state({"access_token": "t", "refreshed_at": NOW - timedelta(days=3)}, NOW) == "fresh"
        assert token_state({"access_token": "t", "refreshed_at": NOW - timedelta(days=8)}, NOW) == "refresh"
        assert token_state({"access_token": "t"}, NOW) == "refresh"
        assert token_state({"access_token": "t", "expires_at": NOW - timedelta(minutes=1)}, NOW) == "expired"

    def test_expiry_falls_back_to_sixty_days_from_refresh(self) -> None:
        assert expires_at({"refreshed_at": NOW}) == NOW + timedelta(days=60)
        assert expires_at({"expires_at": "2026-12-01T00:00:00Z"}) == datetime(2026, 12, 1, tzinfo=timezone.utc)

    def test_warns_only_inside_the_window(self) -> None:
        assert should_warn({"expires_at": NOW + timedelta(days=5)}, NOW)
        assert not should_warn({"expires_at": NOW + timedelta(days=30)}, NOW)
        assert not should_warn({"expires_at": NOW - timedelta(days=1)}, NOW)


class TestInsightsParse:
    def test_post_insights_keeps_only_reported_numbers(self) -> None:
        body = {"data": [{"name": "views", "values": [{"value": 120}]}, {"name": "likes", "values": []}]}
        assert post_insights(body) == {"views": 120.0}

    def test_daily_views_belong_to_the_day_before_end_time(self) -> None:
        body = {"data": [{"name": "views", "values": [{"value": 5, "end_time": "2026-10-22T07:00:00+0000"}]}]}
        assert account_daily_views(body) == {"2026-10-21": 5.0}

    def test_totals_and_demographics(self) -> None:
        assert account_totals({"data": [{"name": "followers_count", "total_value": {"value": 67}}]}) == {
            "followers_count": 67.0
        }
        body = {
            "data": [
                {
                    "total_value": {
                        "breakdowns": [
                            {
                                "results": [
                                    {"dimension_values": ["IN"], "value": 10},
                                    {"dimension_values": ["US"], "value": 30},
                                ]
                            }
                        ]
                    }
                }
            ]
        }
        assert demographics(body) == [{"value": "US", "count": 30}, {"value": "IN", "count": 10}]
