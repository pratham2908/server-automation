"""Rows the owner recalled from memory must never feed a sum, a rate or a graduation."""

import re
from pathlib import Path

from app.services.instagram_reel_snapshot import MEASURED_ONLY, RECALLED_SOURCE

APP = Path(__file__).resolve().parent.parent / "app"


def test_the_filter_drops_recalled_rows_only():
    assert MEASURED_ONLY == {"source": {"$ne": "owner-recalled"}} and RECALLED_SOURCE == "owner-recalled"


def test_every_read_of_the_snapshot_collection_applies_the_filter():
    reads = 0
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"instagram_reel_snapshots\.find(_one)?\(", text):
            reads += 1
            # The query is the first argument: a few lines after the call is enough to find the filter.
            assert "MEASURED_ONLY" in text[match.start() : match.start() + 400], f"{path.name} reads unfiltered"
    assert reads >= 4  # the guard really looked at the known readers
