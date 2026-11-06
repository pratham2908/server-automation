"""The DMs' city list follows the site's: read hourly, the last good list kept when it can't be read."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import requests

from app.services import city_catalog, meet_city


@pytest.fixture(autouse=True)
def _builtin_list() -> Iterator[None]:
    meet_city.set_cities(meet_city.BUILTIN_CITIES)
    yield
    meet_city.set_cities(meet_city.BUILTIN_CITIES)


def _site(*extra: tuple[str, str]) -> dict[str, Any]:
    cities = [{"slug": s, "name": n} for s, n in meet_city.BUILTIN_CITIES.items() if s != "gurugram"]
    return {"cities": cities + [{"slug": s, "name": n} for s, n in extra]}


def test_the_sites_list_is_read_as_slug_to_name() -> None:
    parsed = city_catalog.parse(_site(("lagos", "Lagos")))
    assert parsed is not None and parsed["lagos"] == "Lagos" and parsed["new-york"] == "New York"


@pytest.mark.parametrize(
    "body",
    [None, [], {"cities": "paris"}, {"cities": [{"slug": "paris", "name": "Paris"}]}, {"data": []}],
)
def test_anything_else_is_refused_rather_than_shrinking_the_list(body: Any) -> None:
    assert city_catalog.parse(body) is None


def test_bad_rows_are_skipped_not_fatal() -> None:
    body = _site()
    body["cities"] += [{"slug": "Bad Slug", "name": "X"}, {"slug": "ok", "name": ""}, "junk"]
    parsed = city_catalog.parse(body)
    assert parsed is not None and "ok" not in parsed and "Bad Slug" not in parsed


@pytest.mark.asyncio
async def test_a_city_going_live_on_the_site_is_matched_after_the_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    assert meet_city.read_answer("Lagos").outcome == "unlisted"
    monkeypatch.setattr(city_catalog, "_fetch", lambda: _site(("lagos", "Lagos")))
    assert await city_catalog.refresh() is True
    lagos = meet_city.read_answer("Lagos")
    assert (lagos.outcome, lagos.slug, lagos.name) == ("matched", "lagos", "Lagos")
    # Gurugram is off this site list, so its alias goes quiet rather than pointing at a missing map.
    assert meet_city.read_answer("gurgaon").outcome == "unlisted"


@pytest.mark.asyncio
async def test_a_site_that_cant_be_read_leaves_the_list_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    def down() -> Any:
        raise requests.ConnectionError("site down")

    monkeypatch.setattr(city_catalog, "_fetch", down)
    assert await city_catalog.refresh() is False
    monkeypatch.setattr(city_catalog, "_fetch", lambda: {"cities": []})
    assert await city_catalog.refresh() is False
    assert meet_city.catalog().cities == meet_city.BUILTIN_CITIES


def test_the_builtin_list_has_the_eleven_added_on_4_and_5_november() -> None:
    added = {"sydney", "madrid", "toronto", "jakarta", "istanbul", "ahmedabad", "chandigarh", "gurugram", "kochi"}
    assert added | {"noida", "thiruvananthapuram"} <= set(meet_city.BUILTIN_CITIES)
    assert len(meet_city.BUILTIN_CITIES) == 34
