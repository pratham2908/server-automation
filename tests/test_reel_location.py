"""A reel's location tag: validation, what a schedule request writes, and what goes to Instagram."""

from __future__ import annotations

import pytest

from app.services.reel_location import api_location_id, is_location_refusal, location_fields, validate_place_id


def test_a_place_id_must_be_a_number_and_blank_means_none() -> None:
    assert validate_place_id(" 106078429431815 ") == "106078429431815"
    assert validate_place_id("  ") is None
    assert validate_place_id(None) is None
    with pytest.raises(ValueError, match="must be a number"):
        validate_place_id("London")


def test_a_request_that_says_nothing_writes_nothing() -> None:
    assert location_fields(None, None, "instagram") == {}


def test_empty_strings_clear_and_values_set() -> None:
    assert location_fields("", "", "instagram") == {"location_id": None, "location_name": None}
    assert location_fields("106078429431815", " London, UK ", "instagram") == {
        "location_id": "106078429431815",
        "location_name": "London, UK",
    }


def test_the_name_and_id_are_independent() -> None:
    assert location_fields(None, "London", "instagram") == {"location_name": "London"}
    assert location_fields("106078429431815", None, "instagram") == {"location_id": "106078429431815"}


def test_only_instagram_takes_a_location_but_clearing_is_harmless() -> None:
    with pytest.raises(ValueError, match="Instagram"):
        location_fields("106078429431815", None, "youtube")
    assert location_fields("", "", "youtube") == {"location_id": None, "location_name": None}


def test_a_name_that_is_too_long_is_refused() -> None:
    with pytest.raises(ValueError, match="longer"):
        location_fields(None, "x" * 121, "instagram")


def test_only_a_numeric_id_is_sent_to_instagram() -> None:
    assert api_location_id({"location_id": "106078429431815"}) == "106078429431815"
    assert api_location_id({"location_id": "London"}) is None
    assert api_location_id({"location_name": "London"}) is None
    assert api_location_id({}) is None


def test_only_a_location_complaint_counts_as_a_location_refusal() -> None:
    assert is_location_refusal("Param location_id is not a valid location page ID")
    assert not is_location_refusal("Too many hashtags used")
