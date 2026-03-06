"""One-time migration: rename old video statuses to new values.

Run via:  curl -X POST http://localhost:8000/api/v1/migrate/statuses -H "X-API-Key: your-api-key"
"""

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key

router = APIRouter(
    prefix="/api/v1/migrate",
    tags=["migration"],
    dependencies=[Depends(verify_api_key)],
)


@router.post("/statuses")
async def migrate_statuses(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Rename old video statuses and move video_queue → posting_queue."""
    results = {}

    # 1. done → published
    r = await db.videos.update_many({"status": "done"}, {"$set": {"status": "published"}})
    results["done_to_published"] = r.modified_count

    # 2. in_queue → ready
    r = await db.videos.update_many({"status": "in_queue"}, {"$set": {"status": "ready"}})
    results["in_queue_to_ready"] = r.modified_count

    # 3. Move video_queue → posting_queue
    collections = await db.list_collection_names()
    if "video_queue" in collections:
        entries = await db.video_queue.find().to_list(length=None)
        if entries:
            for e in entries:
                e.pop("_id", None)
            await db.posting_queue.insert_many(entries)
            results["video_queue_copied"] = len(entries)
        else:
            results["video_queue_copied"] = 0

        await db.video_queue.drop()
        results["video_queue_dropped"] = True
    else:
        results["video_queue_dropped"] = "not_found"

    # 4. Summary
    summary = {}
    for s in ["todo", "ready", "scheduled", "published"]:
        summary[s] = await db.videos.count_documents({"status": s})

    return {"ok": True, "changes": results, "final_counts": summary}
