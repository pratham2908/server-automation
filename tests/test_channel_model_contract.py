"""The Channel model must describe what is actually stored and served.

The model had drifted: eleven fields the frontend reads — subscriber_count,
default_tags, competitors and friends — were absent from it. Nothing broke
because no channel route sets ``response_model``, so raw Mongo dicts pass
straight through. The moment someone adds ``response_model=Channel``, an
entirely reasonable change, Pydantic silently drops every undeclared field and
the frontend loses data with no error anywhere.

These tests fail loudly if the model drifts again.
"""

import pytest

from app.models.channel import Channel

# Read by the frontend's BackendChannel interface (analyzer/src/lib/api/types.ts).
FRONTEND_FIELDS = [
    "channel_id",
    "name",
    "platform",
    "youtube_channel_id",
    "instagram_user_id",
    "custom_url",
    "handle",
    "description",
    "subscriber_count",
    "video_count",
    "view_count",
    "default_description",
    "default_tags",
    "competitors",
    "content_schema",
    "automation_config",
    "last_tasks",
    "paused",
    "api_key_prefix",
    "api_key_created_at",
    "created_at",
    "updated_at",
]

# Must never reach a client.
SECRET_FIELDS = ["youtube_tokens", "instagram_tokens", "api_key_hash"]


@pytest.mark.parametrize("field", FRONTEND_FIELDS)
def test_model_declares_field_the_frontend_reads(field):
    assert field in Channel.model_fields, (
        f"Channel.{field} is read by the frontend but missing from the model. "
        "With response_model=Channel it would be silently stripped."
    )


def test_serialising_a_full_document_preserves_frontend_fields():
    """The failure mode itself: round-trip through the model, lose nothing."""
    doc = {
        "channel_id": "c1",
        "name": "Test",
        "platform": "youtube",
        "subscriber_count": 1234,
        "video_count": 56,
        "view_count": 789,
        "default_description": "desc template",
        "default_tags": ["a", "b"],
        "custom_url": "@test",
        "handle": "test",
        "description": "about",
        "content_schema": [{"name": "x"}],
        "paused": True,
    }
    dumped = Channel(**doc).model_dump()
    for key, value in doc.items():
        assert dumped[key] == value, f"{key} was altered or dropped by the model"


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_secrets_default_to_none_so_they_are_never_invented(field):
    ch = Channel(channel_id="c1", name="Test")
    assert getattr(ch, field) is None


def test_optional_fields_have_defaults():
    """A minimal document must validate — most stored channels lack most fields."""
    ch = Channel(channel_id="c1", name="Test")
    assert ch.default_tags == []
    assert ch.competitors == []
    assert ch.content_schema == []
    assert ch.paused is False
