"""One-off: rebuild past hourly channel totals from the per-video readings we already kept.

Channel totals (``channel_view_snapshots``) start with the sync that first sums
every post. Before that, each sync kept readings for some videos only — the
new ones hourly, older ones about daily — so a past total is rebuilt as the sum
of every live video's views at that sync time, interpolating the less-often-read
ones across their gap (``view_series.reconstruct_channel_totals``). Rows are
marked ``backfilled: True`` and only views are rebuilt.

The rebuilt sum covers only the reels/videos we kept readings for, while a live
total covers every post (image posts, videos never read, archived ones). So the
rebuilt series is shifted by one constant so it meets the first live total
exactly — otherwise the hour where the two join would show that difference as a
spike. The posts it stands in for are old and barely move, so a constant is fair.

Run it after the new code has done at least one sync (that first live total is
the anchor). Idempotent: a channel that already has backfilled rows is skipped.
Only times before the first live total are written.

    python scripts/backfill_channel_view_snapshots.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.database import connect_db, get_channel_platform  # noqa: E402
from app.services.instagram_content_analysis import parse_published_at  # noqa: E402
from app.services.instagram_follow_attribution import ViewReading  # noqa: E402
from app.services.instagram_reel_snapshot import MEASURED_ONLY  # noqa: E402
from app.services.view_series import reconstruct_channel_totals  # noqa: E402
from app.timezone import assume_utc  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would be written, write nothing")
    args = parser.parse_args()

    settings = get_settings()
    db = await connect_db(settings.MONGODB_URI, settings.MONGODB_DB_NAME, create_indexes=False)

    async for channel in db.channels.find(
        {}, {"channel_id": 1, "platform": 1, "instagram_user_id": 1, "youtube_channel_id": 1}
    ):
        channel_id = channel["channel_id"]
        platform = get_channel_platform(channel)
        id_field, collection, key = (
            ("instagram_media_id", db.instagram_reel_snapshots, "media_id")
            if platform == "instagram"
            else ("youtube_video_id", db.youtube_video_snapshots, "youtube_video_id")
        )
        if await db.channel_view_snapshots.count_documents({"channel_id": channel_id, "backfilled": True}):
            print(f"  {channel_id:<28} already backfilled — skipped")
            continue

        published = {
            doc[id_field]: parse_published_at(doc.get("published_at"))
            async for doc in db.videos.find(
                {"channel_id": channel_id, id_field: {"$ne": None}, "status": "published"},
                {id_field: 1, "published_at": 1},
            )
        }
        query = {"channel_id": channel_id, key: {"$in": list(published)}}
        if platform == "instagram":
            query.update(MEASURED_ONLY)
        readings: dict[str, list[ViewReading]] = {}
        async for snap in collection.find(query, {key: 1, "at": 1, "views": 1}):
            if isinstance(snap.get("views"), int | float):
                readings.setdefault(snap[key], []).append(ViewReading(assume_utc(snap["at"]), float(snap["views"])))
        if not readings:
            continue

        first_live = await db.channel_view_snapshots.find_one(
            {"channel_id": channel_id, "backfilled": {"$ne": True}}, sort=[("at", 1)]
        )
        if not first_live:
            print(f"  {channel_id:<28} no live total yet — run after a sync")
            continue
        anchor_at = assume_utc(first_live["at"])
        times = sorted({r.at for rs in readings.values() for r in rs if r.at < anchor_at} | {anchor_at})
        rebuilt = reconstruct_channel_totals(readings, {k: published.get(k) for k in readings}, times)
        if not rebuilt or rebuilt[-1][0] != anchor_at:
            print(f"  {channel_id:<28} can't rebuild the anchor time — skipped")
            continue
        offset = float(first_live["views"]) - rebuilt[-1][1]
        rebuilt = [(t, v + offset) for t, v in rebuilt[:-1]]
        span = f"{rebuilt[0][0]:%m-%d %H:%M} → {rebuilt[-1][0]:%m-%d %H:%M} UTC" if rebuilt else "nothing"
        print(
            f"  {channel_id:<28} {platform:<9} read={len(readings)}/{len(published)} "
            f"offset={offset:,.0f} rebuilt={len(rebuilt)} {span}"
        )
        if args.dry_run or not rebuilt:
            continue
        await db.channel_view_snapshots.insert_many(
            [
                {"channel_id": channel_id, "platform": platform, "at": t, "views": round(v), "backfilled": True}
                for t, v in rebuilt
            ]
        )

    if args.dry_run:
        print("\nDRY RUN — nothing written.")


if __name__ == "__main__":
    asyncio.run(main())
