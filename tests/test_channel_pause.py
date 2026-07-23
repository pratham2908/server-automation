"""Channel pause — background crons skip paused channels.

The important case is the absent field. ``paused`` was added after every
existing channel document was written, so a filter of ``{"paused": False}``
would match none of them and silently halt all background work. Both helpers
must treat "no such key" as active.
"""

import pytest

from app.database import is_channel_paused, not_paused_query

ACTIVE = {"channel_id": "a", "paused": False}
PAUSED = {"channel_id": "b", "paused": True}
LEGACY = {"channel_id": "c"}  # written before the field existed


def matches(query: dict, doc: dict) -> bool:
    """Evaluate the subset of Mongo query syntax this filter uses."""
    for field, cond in query.items():
        value = doc.get(field)
        if isinstance(cond, dict):
            if "$ne" in cond and value == cond["$ne"]:
                return False
        elif value != cond:
            return False
    return True


class TestNotPausedQuery:
    def test_selects_active_channel(self):
        assert matches(not_paused_query(), ACTIVE)

    def test_selects_legacy_channel_without_the_field(self):
        """The regression that would take down every cron at once."""
        assert matches(not_paused_query(), LEGACY)

    def test_excludes_paused_channel(self):
        assert not matches(not_paused_query(), PAUSED)

    def test_uses_ne_not_equality(self):
        """`{"paused": False}` would exclude every pre-existing channel."""
        assert not_paused_query() == {"paused": {"$ne": True}}


class TestIsChannelPaused:
    @pytest.mark.parametrize(
        ("doc", "expected"),
        [(ACTIVE, False), (PAUSED, True), (LEGACY, False), (None, False), ({}, False)],
    )
    def test_across_document_shapes(self, doc, expected):
        assert is_channel_paused(doc) is expected


class TestCronsUseTheFilter:
    """Every cron-level channel listing must go through the shared helper."""

    @pytest.mark.parametrize(
        "module",
        [
            "sync_analysis_cron",
            "growth_cron",
            "comment_analysis_cron",
            "comment_reply_cron",
        ],
    )
    def test_cron_filters_channels(self, module):
        import importlib
        import inspect

        src = inspect.getsource(importlib.import_module(f"app.services.{module}"))
        assert "db.channels.find(not_paused_query())" in src, (
            f"{module} must filter paused channels"
        )
        assert "db.channels.find()" not in src, (
            f"{module} still has an unfiltered channel listing"
        )

    def test_auto_publisher_guards_and_keeps_the_video_queued(self):
        import inspect

        from app.services import auto_publisher

        src = inspect.getsource(auto_publisher)
        assert "is_channel_paused(channel_doc)" in src

        # The paused branch must not delete the queue entry: the video has to
        # still be there when the channel is resumed.
        guard = src.split("is_channel_paused(channel_doc)")[1].split("continue")[0]
        assert "delete_one" not in guard, (
            "paused channels must not have their queued videos deleted"
        )


class TestChannelModel:
    def test_defaults_to_not_paused(self):
        from app.models.channel import Channel

        ch = Channel(channel_id="x", name="X")
        assert ch.paused is False
