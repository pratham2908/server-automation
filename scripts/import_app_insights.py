"""Per-reel insights from a screen recording of the Instagram app, into the reels they belong to.

Two steps, because the text recognition needs macOS and the database is on the server:

  # on the Mac: frames -> OCR -> one record per reel (writes <out>.json and the raw <out>.ocr.txt)
  python scripts/import_app_insights.py extract "ScreenRecording_11-17-2026 15-45-35_1.MP4" out.json

  # on the server: match each record to a stored reel and keep it
  PYTHONPATH=. venv/bin/python scripts/import_app_insights.py import out.json DreamScenicAi [--dry-run]

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

from app.services.app_insights import ReelInsights, parse_ocr, read_reels
from app.timezone import IST

OCR_SOURCE = Path(__file__).with_name("app_insights_ocr.swift")
FRAMES_PER_SECOND = 2  # a screen held for half a second is read at least once
FRAME_WIDTH = 590  # half an iPhone screen: OCR reads every number, at a quarter of the work


def recorded_at(video: Path) -> datetime:
    """iOS names recordings "ScreenRecording_11-17-2026 15-45-35"; else the file's own time."""
    found = re.search(r"(\d{2}-\d{2}-\d{4}) (\d{2}-\d{2}-\d{2})", video.name)
    if found:
        return datetime.strptime(f"{found[1]} {found[2]}", "%m-%d-%Y %H-%M-%S").replace(tzinfo=IST)
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


async def import_(path: Path, channel_id: str, dry_run: bool) -> None:
    import certifi
    from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

    from app.config import get_settings
    from app.services.app_insights import match_reel
    from app.services.app_insights_store import channel_reels, store

    payload = json.loads(path.read_text())
    records = [ReelInsights(**r) for r in payload["reels"]]
    captured_at = datetime.fromisoformat(payload["recorded_at"])
    settings = get_settings()
    db: AsyncIOMotorDatabase = AsyncIOMotorClient(settings.MONGODB_URI, tlsCAFile=certifi.where())[
        settings.MONGODB_DB_NAME
    ]
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
    args = parser.parse_args(argv)
    if args.cmd == "extract":
        extract(args.video, args.out)
    else:
        asyncio.run(import_(args.json, args.channel_id, args.dry_run))


if __name__ == "__main__":
    main(sys.argv[1:])
