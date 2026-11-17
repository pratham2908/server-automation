"""Per-reel insights from a screen recording of the Instagram app, into the reels they belong to.

Two steps, because the text recognition needs macOS and the database is on the server:

  # on the Mac: frames -> OCR -> one record per reel (writes <out>.json and the raw <out>.ocr.txt)
  python scripts/import_app_insights.py extract "ScreenRecording_11-17-2026 15-45-35_1.MP4" out.json

  # on the server: match each record to a stored reel and keep it
  PYTHONPATH=. venv/bin/python scripts/import_app_insights.py import out.json DreamScenicAi [--dry-run]

  # which reels to record next: grown >20% since their last recording, or never recorded and >100K
  PYTHONPATH=. venv/bin/python scripts/import_app_insights.py due DreamScenicAi

In the recording, open each reel's "Reel insights": Overview (the view count, then scroll to "Top
sources of views"), Engagement, and Audience (countries, and scroll the whole age list). Hold each
screen about a second. A reel is matched by its view count, so the Overview tab matters.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.services.app_insights import ReelInsights, parse_ocr, read_reels
from app.timezone import IST

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorDatabase

OCR_SOURCE = Path(__file__).with_name("app_insights_ocr.swift")
FRAMES_PER_SECOND = 2  # a screen held for half a second is read at least once
FRAME_WIDTH = 590  # half an iPhone screen: OCR reads every number, at a quarter of the work


def recorded_at(video: Path) -> datetime:
    """When recording started, from the name: iOS's "ScreenRecording_11-17-2026 15-45-35" or the
    archived "city_solved-2026-11-17-15-45". Else the file's own time, which is when it was copied."""
    found = re.search(r"(\d{2}-\d{2}-\d{4}) (\d{2}-\d{2}-\d{2})", video.name)
    if found:
        return datetime.strptime(f"{found[1]} {found[2]}", "%m-%d-%Y %H-%M-%S").replace(tzinfo=IST)
    found = re.search(r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2})", video.name)
    if found:
        return datetime.strptime(found[1], "%Y-%m-%d-%H-%M").replace(tzinfo=IST)
    return datetime.fromtimestamp(video.stat().st_mtime, tz=IST)


def extract(video: Path, out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        frames, binary = Path(tmp) / "frames", Path(tmp) / "ocr"
        frames.mkdir()
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(video),
                "-vf",
                f"fps={FRAMES_PER_SECOND},scale={FRAME_WIDTH}:-2",
                "-q:v",
                "3",
                str(frames / "f_%04d.jpg"),
            ],
            check=True,
        )
        subprocess.run(["swiftc", "-O", str(OCR_SOURCE), "-o", str(binary)], check=True)
        text = subprocess.run([str(binary), str(frames)], check=True, capture_output=True, text=True).stdout
    out.with_suffix(".ocr.txt").write_text(text)
    records = read_reels(parse_ocr(text))
    payload = {
        "video": video.name,
        "recorded_at": recorded_at(video).isoformat(),
        "reels": [asdict(r) for r in records],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    for rec in records:
        print(f"{(rec.thumbnail_text or ['?'])[0]:14} {rec.counts}")
    print(f"{len(records)} reels -> {out}")


def _db() -> AsyncIOMotorDatabase:
    import certifi
    from motor.motor_asyncio import AsyncIOMotorClient

    from app.config import get_settings

    settings = get_settings()
    db: AsyncIOMotorDatabase = AsyncIOMotorClient(settings.MONGODB_URI, tlsCAFile=certifi.where())[
        settings.MONGODB_DB_NAME
    ]
    return db


async def due(channel_id: str, threshold: float, min_views: int) -> None:
    from app.services.app_insights_store import due_reels

    reels = await due_reels(_db(), channel_id, threshold, min_views)
    grown = [d for d in reels if d.recorded_at is not None]
    never = [d for d in reels if d.recorded_at is None]
    print(f"Record again ({len(grown)}): views up more than {threshold:.0%} since the last recording")
    for d in grown:
        change = f"+{d.growth:.0%}" if d.growth is not None else "no view count recorded"
        when = d.recorded_at.strftime("%m-%d") if d.recorded_at else "?"
        print(f"  {change:>6}  {d.title[:60]}  ({d.views_recorded or 0:,} on {when} -> {d.views_now:,})")
    print(f"Never recorded, over {min_views:,} views ({len(never)})")
    for d in never:
        print(f"  {d.views_now:>9,}  {d.title[:60]}")


async def import_(path: Path, channel_id: str, dry_run: bool) -> None:
    from app.services.app_insights import match_reel
    from app.services.app_insights_store import channel_reels, store

    payload = json.loads(path.read_text())
    records = [ReelInsights(**r) for r in payload["reels"]]
    captured_at = datetime.fromisoformat(payload["recorded_at"])
    db = _db()
    if dry_run:
        reels = {r["media_id"]: r for r in await channel_reels(db, channel_id)}
        for rec in records:
            media_id = match_reel(rec, list(reels.values()))
            title = str(reels[media_id]["title"])[:60] if media_id else "NO MATCH"
            print(f"{(rec.thumbnail_text or ['?'])[0]:14} -> {title}")
        return
    result = await store(db, channel_id, records, captured_at, source=payload["video"])
    for media_id, title in result.stored:
        print(f"stored {media_id}  {title[:70]}")
    for rec in result.unmatched:
        print(f"NOT STORED (no clear reel): {rec.thumbnail_text} {rec.counts}")
    print()
    await due(channel_id, 0.20, 100_000)


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract")
    ex.add_argument("video", type=Path)
    ex.add_argument("out", type=Path)
    im = sub.add_parser("import")
    im.add_argument("json", type=Path)
    im.add_argument("channel_id")
    im.add_argument("--dry-run", action="store_true")
    du = sub.add_parser("due")
    du.add_argument("channel_id")
    du.add_argument("--threshold", type=float, default=0.20, help="views growth that makes a reel due")
    du.add_argument("--min-views", type=int, default=100_000, help="a never-recorded reel is due above this")
    args = parser.parse_args(argv)
    if args.cmd == "extract":
        extract(args.video, args.out)
    elif args.cmd == "due":
        asyncio.run(due(args.channel_id, args.threshold, args.min_views))
    else:
        asyncio.run(import_(args.json, args.channel_id, args.dry_run))


if __name__ == "__main__":
    main(sys.argv[1:])
