#!/usr/bin/env python3
"""Backfill repost_count and repost_index for videos.

Fixes the inconsistency where videos have is_repost=True / original_video_id set
but the original video's repost_count is 0 (or missing), and repost_index may
also be unset on the repost entries themselves.

What it does:
  1. Finds every video where is_repost=True and original_video_id is non-null.
  2. Groups them by original_video_id.
  3. Sets repost_count on each original to the actual number of its reposts.
  4. Sets repost_index on each repost (ordered by created_at ascending: 1, 2, 3 ...).
  5. Zeros out repost_count on videos that have no reposts pointing at them.

Usage:
  python backfill_repost_counts.py          # dry-run (read-only, just prints)
  python backfill_repost_counts.py --fix    # apply the fixes
"""

import asyncio
import sys
from collections import defaultdict

sys.path.insert(0, ".")

DRY_RUN = "--fix" not in sys.argv


async def main() -> None:
    from app.config import get_settings
    from app.database import close_db, connect_db
    from app.timezone import now_ist

    settings = get_settings()
    db = await connect_db(settings.MONGODB_URI, settings.MONGODB_DB_NAME, create_indexes=False)

    mode = "DRY RUN (pass --fix to apply)" if DRY_RUN else "APPLYING FIXES"
    print(f"\n{'=' * 60}")
    print(f"  Repost Backfill — {mode}")
    print(f"{'=' * 60}\n")

    # ------------------------------------------------------------------
    # 1. Load all repost videos (is_repost=True, original_video_id set)
    # ------------------------------------------------------------------
    repost_docs = await db.videos.find(
        {"is_repost": True, "original_video_id": {"$ne": None}},
        {"video_id": 1, "original_video_id": 1, "repost_index": 1, "created_at": 1, "title": 1},
    ).to_list(length=None)

    print(f"Found {len(repost_docs)} video(s) marked as reposts.\n")

    if not repost_docs:
        print("Nothing to backfill.")
        await close_db()
        return

    # ------------------------------------------------------------------
    # 2. Group reposts by original_video_id
    # ------------------------------------------------------------------
    groups: dict[str, list[dict]] = defaultdict(list)
    for doc in repost_docs:
        groups[doc["original_video_id"]].append(doc)

    print(f"Distinct originals referenced: {len(groups)}\n")

    now = now_ist()
    total_originals_fixed = 0
    total_reposts_indexed = 0

    for original_id, reposts in groups.items():
        # Sort reposts by creation time so index assignment is stable
        reposts.sort(key=lambda d: d.get("created_at") or "")

        # Fetch current state of original
        original = await db.videos.find_one(
            {"video_id": original_id},
            {"video_id": 1, "title": 1, "repost_count": 1},
        )

        current_count = (original or {}).get("repost_count", 0) or 0
        correct_count = len(reposts)

        orig_title = (original or {}).get("title", "<not found>")
        print(f"  Original: {original_id}  \"{orig_title}\"")
        print(f"    repost_count in DB : {current_count}")
        print(f"    actual reposts     : {correct_count}")

        if original is None:
            print("    WARNING: original video not found in DB — skipping count fix\n")
        elif current_count != correct_count:
            print(f"    → fix repost_count {current_count} → {correct_count}")
            if not DRY_RUN:
                await db.videos.update_one(
                    {"video_id": original_id},
                    {"$set": {"repost_count": correct_count, "updated_at": now}},
                )
            total_originals_fixed += 1
        else:
            print("    ✓ repost_count already correct")

        # Assign / fix repost_index on each repost
        for idx, repost in enumerate(reposts, start=1):
            current_index = repost.get("repost_index")
            repost_title = repost.get("title", "")
            if current_index != idx:
                print(f"    repost {repost['video_id']}  \"{repost_title}\"")
                print(f"      → repost_index {current_index} → {idx}")
                if not DRY_RUN:
                    await db.videos.update_one(
                        {"video_id": repost["video_id"]},
                        {"$set": {"repost_index": idx, "updated_at": now}},
                    )
                total_reposts_indexed += 1
            else:
                print(f"    repost {repost['video_id']}  \"{repost_title}\"  ✓ index={idx}")

        print()

    # ------------------------------------------------------------------
    # 3. Zero out repost_count on originals that have no reposts at all
    # ------------------------------------------------------------------
    orphaned_cursor = db.videos.find(
        {
            "is_repost": {"$ne": True},
            "repost_count": {"$gt": 0},
        },
        {"video_id": 1, "title": 1, "repost_count": 1},
    )
    orphaned = await orphaned_cursor.to_list(length=None)

    # Only flag ones not in our groups map (they genuinely have no reposts)
    truly_orphaned = [o for o in orphaned if o["video_id"] not in groups]

    if truly_orphaned:
        print(f"Videos with repost_count > 0 but no reposts pointing at them: {len(truly_orphaned)}")
        for o in truly_orphaned:
            print(f"  {o['video_id']}  \"{o.get('title','')}\"  repost_count={o['repost_count']} → 0")
            if not DRY_RUN:
                await db.videos.update_one(
                    {"video_id": o["video_id"]},
                    {"$set": {"repost_count": 0, "updated_at": now}},
                )
        print()

    # ------------------------------------------------------------------
    # 4. Summary
    # ------------------------------------------------------------------
    print(f"{'=' * 60}")
    if DRY_RUN:
        print("  DRY RUN complete — no changes written.")
        print(f"  Originals that would be fixed : {total_originals_fixed}")
        print(f"  Reposts that would be re-indexed : {total_reposts_indexed}")
        print(f"  Orphaned counts that would be zeroed : {len(truly_orphaned)}")
        print("  Run with --fix to apply.")
    else:
        print("  Done.")
        print(f"  Originals fixed       : {total_originals_fixed}")
        print(f"  Reposts re-indexed    : {total_reposts_indexed}")
        print(f"  Orphaned counts zeroed: {len(truly_orphaned)}")
    print(f"{'=' * 60}\n")

    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
