"""Keeps the DMs' city list the same as the meet tool's: read from the site every hour.

The site's REAL_CITIES grows as maps are added (11 on 4-5 Nov), and a hand-copied list on this side
went stale at once: people who asked for Sydney, Madrid and Istanbul were told "on our list" while
the maps were live. So the site publishes its list and this reads it into `meet_city.set_cities`.

Until the site serves it, or whenever it can't be read or looks wrong, the last good list stays in
use (at first, `meet_city.BUILTIN_CITIES`). A bad answer never shrinks the list to nothing.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import requests

from app.logger import get_logger
from app.services import meet_city

logger = get_logger(__name__)

CITY_LIST_URL = "https://tryalgoviz.com/api/public/meet-cities/"
REFRESH = timedelta(hours=1)
TIMEOUT_SECONDS = 15
# The site has had 23+ cities since October; a list much shorter than that is a broken answer, not a removal.
MIN_CITIES = 10
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def parse(body: Any) -> dict[str, str] | None:
    """slug -> name from ``{"cities": [{"slug": "paris", "name": "Paris"}, ...]}``, or None if it isn't that."""
    items = body.get("cities") if isinstance(body, Mapping) else None
    if not isinstance(items, list):
        return None
    cities: dict[str, str] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        slug, name = item.get("slug"), item.get("name")
        if isinstance(slug, str) and SLUG.match(slug) and isinstance(name, str) and name.strip():
            cities[slug] = name.strip()
    return cities if len(cities) >= MIN_CITIES else None


def _fetch() -> Any:
    response = requests.get(CITY_LIST_URL, timeout=TIMEOUT_SECONDS, headers={"Accept": "application/json"})
    response.raise_for_status()
    return response.json()


async def refresh() -> bool:
    """Read the site's list and use it. False (and the current list kept) when it can't be read."""
    try:
        body = await asyncio.to_thread(_fetch)
    except (requests.RequestException, ValueError) as exc:
        # Expected until the site serves the route, and on any outage: the current list stays.
        logger.info(
            "[CityCatalog] Site city list not read (%s); keeping %d cities", exc, len(meet_city.catalog().cities)
        )
        return False
    cities = parse(body)
    if cities is None:
        logger.warning("[CityCatalog] Site city list is malformed or too short; keeping the current one")
        return False
    before = set(meet_city.catalog().cities)
    meet_city.set_cities(cities)
    added, removed = sorted(set(cities) - before), sorted(before - set(cities))
    if added or removed:
        logger.info("[CityCatalog] %d cities from the site (added %s, removed %s)", len(cities), added, removed)
    return True


async def run_city_catalog() -> None:
    """Long-running loop: refresh at start, then every hour."""
    while True:
        try:
            await refresh()
        except asyncio.CancelledError:
            raise
        await asyncio.sleep(REFRESH.total_seconds())
