import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

async def migrate_errors():
    mongodb_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    db_name = os.getenv("MONGODB_DB_NAME", "youtube_automation")
    
    print(f"Connecting to {mongodb_uri}, DB: {db_name}")
    client = AsyncIOMotorClient(mongodb_uri)
    db = client[db_name]
    
    # Update all documents missing count or last_occurred_at
    cursor = db.errors.find({
        "$or": [
            {"count": {"$exists": False}},
            {"last_occurred_at": {"$exists": False}},
            {"timestamp": {"$exists": False}}
        ]
    })
    
    now = datetime.now(IST)
    updated = 0
    async for doc in cursor:
        upd = {}
        if "count" not in doc:
            upd["count"] = 1
        if "timestamp" not in doc:
            upd["timestamp"] = doc.get("timestamp") or now
        if "last_occurred_at" not in doc:
            upd["last_occurred_at"] = doc.get("timestamp") or now
            
        if upd:
            await db.errors.update_one({"_id": doc["_id"]}, {"$set": upd})
            updated += 1
            
    print(f"Migrated {updated} error documents.")

if __name__ == "__main__":
    asyncio.run(migrate_errors())
