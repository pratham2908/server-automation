"""Fold pre-kind video_sources documents into the typed ``config`` shape.

Sources written before kinds existed carry their settings flat on the document::

    {list_path, detail_path, mark_imported_path, api_key, auth_style}

Every one of those is the export-feed contract, since it was the only one that
existed, so they migrate to ``{"config": {"kind": "georank", ...}}``. Documents
that already have a ``config`` are left alone, so this is safe to re-run.

    python3 scripts/migrate_video_sources.py --dry-run
    python3 scripts/migrate_video_sources.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.database import connect_db  # noqa: E402
from app.models.video_source import GeoRankConfig  # noqa: E402
from app.services.video_sources import mask_secret  # noqa: E402
from app.timezone import now_ist  # noqa: E402

FLAT_FIELDS = ("list_path", "detail_path", "mark_imported_path", "api_key", "auth_style")


async def main() -> int:
    p = argparse.ArgumentParser(description="Migrate flat video_sources docs to typed configs")
    p.add_argument("--dry-run", action="store_true", help="Report what would change and write nothing")
    args = p.parse_args()

    settings = get_settings()
    db = await connect_db(settings.MONGODB_URI, settings.MONGODB_DB_NAME, create_indexes=False)

    docs = await db.video_sources.find({}).to_list(length=None)
    migrated = skipped = failed = 0

    for doc in docs:
        sid = doc.get("source_id")
        if doc.get("config"):
            print(f"  skip   {sid} — already has a config ({doc['config'].get('kind')})")
            skipped += 1
            continue

        try:
            config = GeoRankConfig(
                list_path=doc.get("list_path") or "/api/ext/videos",
                detail_path=doc.get("detail_path") or "/api/ext/videos/{id}",
                # An absent mark path means the app never had one; only a missing
                # *key* falls back to the default.
                mark_imported_path=doc.get("mark_imported_path", "/api/ext/videos/{id}/imported"),
                api_key=doc["api_key"],
                auth_style=doc.get("auth_style") or "bearer",
            )
        except (KeyError, ValueError) as exc:
            print(f"  FAIL   {sid} — cannot build a config from it: {exc}")
            failed += 1
            continue

        print(f"  georank {sid} ({doc.get('name')}) — key {mask_secret(config.api_key)}, {config.list_path}")
        if args.dry_run:
            migrated += 1
            continue

        await db.video_sources.update_one(
            {"_id": doc["_id"]},
            {
                "$set": {"config": config.model_dump(), "updated_at": now_ist()},
                "$unset": dict.fromkeys(FLAT_FIELDS, ""),
            },
        )
        migrated += 1

    verb = "would migrate" if args.dry_run else "migrated"
    print(f"\n{verb} {migrated}, skipped {skipped}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
