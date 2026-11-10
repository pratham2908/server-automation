"""Copy per-reel follows from Business Suite CSV exports into the videos they belong to.

Usage (on the server): PYTHONPATH=. venv/bin/python scripts/import_follows_csv.py export.csv [more.csv ...]

Each file is read in the order given; later files win for a reel listed in several. Instagram's
lifetime count only grows, so pass the newest export last.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

import certifi
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import get_settings
from app.services.follows_import import apply_csv_rows, parse_csv


def export_end(path: Path) -> date:
    """The last day an export covers, from Business Suite's file name ("Jan-01-2026_Nov-10-2026_<id>.csv")."""
    try:
        return datetime.strptime(path.name.split("_")[1], "%b-%d-%Y").date()
    except (IndexError, ValueError):
        return date.today()


async def main(paths: list[Path]) -> None:
    settings = get_settings()
    db = AsyncIOMotorClient(settings.MONGODB_URI, tlsCAFile=certifi.where())[settings.MONGODB_DB_NAME]
    for path in paths:
        rows = parse_csv(path.read_text(encoding="utf-8-sig"))
        as_of = export_end(path)
        result = await apply_csv_rows(db, rows, as_of)
        print(f"{path.name} (to {as_of}): {len(rows)} rows; stored {result.matched}")
        for account, ids in result.not_ours.items():
            print(f"  {account}: {len(ids)} posts with no video of ours: {', '.join(ids)}")


if __name__ == "__main__":
    asyncio.run(main([Path(p) for p in sys.argv[1:]]))
