"""One-off: drop the external creator-app credentials from every channel.

The external API and its ``X-Channel-Api-Key`` are gone. Leaving the hashes in
the documents would keep dead secret material around for no benefit — a key that
nothing can validate is not "safely retained", it is just an unowned secret. This
unsets the three fields wherever they exist.

Idempotent: re-running matches nothing and reports zero.

    python scripts/purge_channel_api_keys.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.database import connect_db  # noqa: E402

_FIELDS = ("api_key_hash", "api_key_prefix", "api_key_created_at")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args()

    settings = get_settings()
    db = await connect_db(settings.MONGODB_URI, settings.MONGODB_DB_NAME, create_indexes=False)

    query = {"$or": [{field: {"$exists": True}} for field in _FIELDS]}
    affected = await db.channels.find(query, {"_id": 0, "channel_id": 1, "api_key_prefix": 1}).to_list(None)

    for doc in affected:
        had_key = doc.get("api_key_prefix")
        print(f"  {doc['channel_id']:<40} {'key issued: ' + had_key if had_key else 'no key issued'}")

    if args.dry_run:
        print(f"\nDRY RUN — {len(affected)} channel document(s) would be updated.")
        return

    result = await db.channels.update_many(query, {"$unset": {field: "" for field in _FIELDS}})
    print(f"\nMatched {result.matched_count}, modified {result.modified_count} channel document(s).")

    left = await db.channels.count_documents(query)
    print(f"Remaining documents holding key fields: {left}")


if __name__ == "__main__":
    asyncio.run(main())
