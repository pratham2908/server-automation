"""Switch on on-demand rendering for a channel's GeoRank source.

The auto-scheduler can ask a source to render a video when a slot has nothing to
post. That capability is off until a source carries a ``generation`` block; this
writes GeoRank's, taken from the app's own contract
(``GEORANK_AUTO_GENERATION``), so nobody has to remember the paths.

It always requests the app's "you choose" mode — GeoRank fixes its house format,
rotates voice and preset, and keeps its highest-scoring idea — so the flag says
``{"auto": true}`` and we never pick the subject.

    python3 scripts/enable_source_generation.py --channel histriphy --dry-run
    python3 scripts/enable_source_generation.py --channel histriphy --eta 15
    python3 scripts/enable_source_generation.py --channel histriphy --disable

Re-running is safe: it overwrites the block with the same values.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.database import connect_db  # noqa: E402
from app.models.video_source import GenerationConfig  # noqa: E402
from app.services.video_sources.georank import GEORANK_AUTO_GENERATION  # noqa: E402
from app.timezone import now_ist  # noqa: E402

# GeoRank renders in 10-15 minutes. The scheduler acts an hour before a slot, so
# this leaves plenty of room; it only needs to be honest, not pessimistic.
DEFAULT_ETA_MINUTES = 15


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", required=True, help="channel_id whose source to update")
    parser.add_argument("--source-id", help="Which source, when the channel has several")
    parser.add_argument("--eta", type=int, default=DEFAULT_ETA_MINUTES, help="Typical render minutes")
    parser.add_argument("--max-per-day", type=int, default=4, help="Cap on renders requested per day")
    parser.add_argument("--disable", action="store_true", help="Remove the capability instead")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, write nothing")
    args = parser.parse_args()

    settings = get_settings()
    # A one-off script has no business building indexes on the live database.
    db = await connect_db(settings.MONGODB_URI, settings.MONGODB_DB_NAME, create_indexes=False)

    query: dict[str, object] = {"channel_id": args.channel, "config.kind": "georank"}
    if args.source_id:
        query["source_id"] = args.source_id

    sources = await db.video_sources.find(query).to_list(length=50)
    if not sources:
        print(f"No GeoRank source found for channel '{args.channel}'.")
        return 1
    if len(sources) > 1 and not args.source_id:
        print("Several GeoRank sources on this channel — pass --source-id for one of:")
        for doc in sources:
            print(f"  {doc['source_id']}  {doc.get('name')}")
        return 1

    source = sources[0]
    if args.disable:
        block = None
    else:
        # Validate before writing: a bad block would otherwise only surface when
        # a slot went looking for something to render.
        block = GenerationConfig(
            eta_minutes=args.eta,
            max_per_day=args.max_per_day,
            **GEORANK_AUTO_GENERATION,
        ).model_dump()

    verb = "Disabling" if args.disable else "Enabling"
    print(f"{verb} generation on '{source.get('name')}' ({source['source_id']}) for {args.channel}")
    if block is not None:
        print(f"  POST {source['base_url']}{block['create_path']} {block['create_body']}")
        print(f"  poll {block['status_path']} until {block['status_field']}=={block['completed_status']}")
        print(f"  eta {block['eta_minutes']}m, at most {block['max_per_day']}/day")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    await db.video_sources.update_one(
        {"source_id": source["source_id"]},
        {"$set": {"config.generation": block, "updated_at": now_ist()}},
    )
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
