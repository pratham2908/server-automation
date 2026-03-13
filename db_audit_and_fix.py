#!/usr/bin/env python3
"""Database audit and backfill script.

Checks for and fixes data inconsistencies introduced before the consistency fixes.
Run from project root with the app's venv activated.

Usage:
  python db_audit_and_fix.py          # audit only (read-only)
  python db_audit_and_fix.py --fix    # audit + apply fixes
"""

import asyncio
import sys

sys.path.insert(0, ".")

DRY_RUN = "--fix" not in sys.argv


async def main() -> None:
    from app.config import get_settings
    from app.database import connect_db, close_db
    from app.timezone import now_ist, UTC
    from datetime import timedelta

    settings = get_settings()
    db = await connect_db(settings.MONGODB_URI, settings.MONGODB_DB_NAME, create_indexes=False)

    mode = "AUDIT ONLY (pass --fix to apply)" if DRY_RUN else "AUDIT + FIX"
    print(f"\n{'='*60}")
    print(f"  Database Audit — {mode}")
    print(f"{'='*60}\n")

    channels = await db.channels.find({}, {"channel_id": 1}).to_list(length=None)
    channel_ids = [c["channel_id"] for c in channels]
    print(f"Channels: {channel_ids}\n")

    # ------------------------------------------------------------------
    # 1. Check for duplicate analysis_history entries
    # ------------------------------------------------------------------
    print("--- 1. Duplicate analysis_history entries ---")
    pipeline = [
        {"$group": {"_id": {"channel_id": "$channel_id", "video_id": "$video_id"}, "count": {"$sum": 1}, "ids": {"$push": "$_id"}}},
        {"$match": {"count": {"$gt": 1}}},
    ]
    duplicates = await db.analysis_history.aggregate(pipeline).to_list(length=None)
    if duplicates:
        print(f"  FOUND {len(duplicates)} duplicate (channel_id, video_id) pairs:")
        for dup in duplicates:
            print(f"    {dup['_id']} — {dup['count']} copies")
        if not DRY_RUN:
            for dup in duplicates:
                ids_to_remove = dup["ids"][1:]  # keep the first, remove the rest
                await db.analysis_history.delete_many({"_id": {"$in": ids_to_remove}})
            print(f"  FIXED: Removed {sum(d['count'] - 1 for d in duplicates)} duplicate records.")
    else:
        print("  OK — no duplicates.")

    # ------------------------------------------------------------------
    # 2. Check for orphaned schedule_queue entries
    # ------------------------------------------------------------------
    print("\n--- 2. Orphaned schedule_queue entries ---")
    orphaned_queue = 0
    all_scheduled_queue = await db.schedule_queue.find({}).to_list(length=None)
    for entry in all_scheduled_queue:
        video = await db.videos.find_one(
            {"channel_id": entry["channel_id"], "video_id": entry["video_id"]},
            {"status": 1},
        )
        if not video or video.get("status") != "scheduled":
            orphaned_queue += 1
            actual = video.get("status", "NOT FOUND") if video else "VIDEO MISSING"
            print(f"  Orphan: channel={entry['channel_id']} video={entry['video_id']} actual_status={actual}")
            if not DRY_RUN:
                await db.schedule_queue.delete_one({"_id": entry["_id"]})
    if orphaned_queue:
        if not DRY_RUN:
            print(f"  FIXED: Removed {orphaned_queue} orphaned schedule_queue entries.")
    else:
        print("  OK — no orphaned entries.")

    # ------------------------------------------------------------------
    # 3. Check for orphaned posting_queue entries
    # ------------------------------------------------------------------
    print("\n--- 3. Orphaned posting_queue entries ---")
    orphaned_posting = 0
    all_posting_queue = await db.posting_queue.find({}).to_list(length=None)
    for entry in all_posting_queue:
        video = await db.videos.find_one(
            {"channel_id": entry["channel_id"], "video_id": entry["video_id"]},
            {"status": 1},
        )
        if not video or video.get("status") != "ready":
            orphaned_posting += 1
            actual = video.get("status", "NOT FOUND") if video else "VIDEO MISSING"
            print(f"  Orphan: channel={entry['channel_id']} video={entry['video_id']} actual_status={actual}")
            if not DRY_RUN:
                await db.posting_queue.delete_one({"_id": entry["_id"]})
    if orphaned_posting:
        if not DRY_RUN:
            print(f"  FIXED: Removed {orphaned_posting} orphaned posting_queue entries.")
    else:
        print("  OK — no orphaned entries.")

    # ------------------------------------------------------------------
    # 4. Recompute all category metadata/video_count/video_ids
    # ------------------------------------------------------------------
    print("\n--- 4. Category metadata/video_count/video_ids recompute ---")
    from app.services.todo_engine import _compute_category_metadata

    for ch_id in channel_ids:
        categories = await db.categories.find({"channel_id": ch_id}).to_list(length=None)
        print(f"  Channel '{ch_id}': {len(categories)} categories")
        for cat in categories:
            name = cat["name"]
            meta = await _compute_category_metadata(ch_id, name, db)
            new_vc = meta.get("total_videos", 0)
            new_vids = meta.get("video_ids", [])
            old_vc = cat.get("video_count", 0)
            old_vids = cat.get("video_ids", [])
            old_meta_total = (cat.get("metadata") or {}).get("total_videos", 0)

            drifted = (
                old_vc != new_vc
                or old_meta_total != new_vc
                or sorted(old_vids) != sorted(new_vids)
            )
            if drifted:
                print(f"    DRIFT '{name}': video_count {old_vc}->{new_vc}, "
                      f"metadata.total_videos {old_meta_total}->{new_vc}, "
                      f"video_ids {len(old_vids)}->{len(new_vids)}")
                if not DRY_RUN:
                    await db.categories.update_one(
                        {"_id": cat["_id"]},
                        {"$set": {
                            "metadata": meta,
                            "video_count": new_vc,
                            "video_ids": new_vids,
                            "updated_at": now_ist(),
                        }},
                    )
            else:
                print(f"    OK '{name}': video_count={new_vc}, video_ids={len(new_vids)}")
        if not DRY_RUN and any(True for cat in categories for _ in [1]):
            print(f"  FIXED: All categories for '{ch_id}' recomputed.")

    # ------------------------------------------------------------------
    # 5. Check analysis.category_analysis for stale/missing category names
    # ------------------------------------------------------------------
    print("\n--- 5. Stale category names in analysis.category_analysis ---")
    for ch_id in channel_ids:
        analysis = await db.analysis.find_one({"channel_id": ch_id})
        if not analysis:
            print(f"  Channel '{ch_id}': no analysis document.")
            continue
        cat_names_in_db = set()
        async for cat in db.categories.find({"channel_id": ch_id}, {"name": 1}):
            cat_names_in_db.add(cat["name"])

        cat_analysis = analysis.get("category_analysis", [])
        stale = []
        for ca in cat_analysis:
            if ca.get("category") not in cat_names_in_db:
                stale.append(ca["category"])
        if stale:
            print(f"  Channel '{ch_id}': analysis references categories not in DB: {stale}")
        else:
            print(f"  Channel '{ch_id}': OK — all {len(cat_analysis)} category names match.")

    # ------------------------------------------------------------------
    # 6. Check for videos referencing non-existent categories
    # ------------------------------------------------------------------
    print("\n--- 6. Videos referencing non-existent categories ---")
    for ch_id in channel_ids:
        cat_names = set()
        async for cat in db.categories.find({"channel_id": ch_id}, {"name": 1}):
            cat_names.add(cat["name"])

        orphan_vids = await db.videos.find(
            {"channel_id": ch_id, "category": {"$nin": list(cat_names), "$ne": ""}},
            {"video_id": 1, "category": 1, "status": 1},
        ).to_list(length=None)
        if orphan_vids:
            print(f"  Channel '{ch_id}': {len(orphan_vids)} videos reference missing categories:")
            by_cat: dict[str, int] = {}
            for v in orphan_vids:
                c = v.get("category", "(empty)")
                by_cat[c] = by_cat.get(c, 0) + 1
            for c, n in by_cat.items():
                print(f"    '{c}': {n} videos")
        else:
            print(f"  Channel '{ch_id}': OK")

    # ------------------------------------------------------------------
    # 7. Check for videos with status='published' but no published_at
    # ------------------------------------------------------------------
    print("\n--- 7. Published videos missing published_at ---")
    missing_pub = await db.videos.count_documents(
        {"status": "published", "published_at": None}
    )
    if missing_pub:
        print(f"  FOUND {missing_pub} published videos with no published_at.")
    else:
        print("  OK")

    # ------------------------------------------------------------------
    # 8. Summary stats
    # ------------------------------------------------------------------
    print(f"\n--- 8. Collection counts ---")
    for coll_name in ["channels", "videos", "categories", "analysis", "analysis_history",
                       "posting_queue", "schedule_queue"]:
        count = await db[coll_name].count_documents({})
        print(f"  {coll_name}: {count}")

    # Video status breakdown
    print(f"\n--- Video status breakdown ---")
    for ch_id in channel_ids:
        pipeline = [
            {"$match": {"channel_id": ch_id}},
            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        results = await db.videos.aggregate(pipeline).to_list(length=None)
        counts = {r["_id"]: r["count"] for r in results}
        print(f"  {ch_id}: {counts}")

    await close_db()
    print(f"\n{'='*60}")
    print(f"  Audit complete.")
    if DRY_RUN:
        print(f"  Re-run with --fix to apply changes.")
    else:
        print(f"  All fixes applied.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
