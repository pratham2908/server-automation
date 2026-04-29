import asyncio
import os
import sys

# Add the current directory to sys.path to allow importing from app
sys.path.append(os.getcwd())

from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timedelta, timezone
from app.config import get_settings

IST = timezone(timedelta(hours=5, minutes=30))

async def migrate_errors():
    settings = get_settings()
    mongodb_uri = settings.MONGODB_URI
    db_name = settings.MONGODB_DB_NAME
    
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
