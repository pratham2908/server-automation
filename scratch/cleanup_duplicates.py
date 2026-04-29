import asyncio
import os
import sys
sys.path.append(os.getcwd())

from motor.motor_asyncio import AsyncIOMotorClient
from app.config import get_settings
from app.timezone import now_ist

async def cleanup_duplicates():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DB_NAME]
    
    pipeline = [
        {"$match": {"resolved": False}},
        {"$group": {
            "_id": {"feature": "$feature", "message": "$message"},
            "count": {"$sum": "$count"},
            "docs": {"$push": "$$ROOT"}
        }},
        {"$match": {"count": {"$gt": 1}}}
    ]
    
    groups = await db.errors.aggregate(pipeline).to_list(length=None)
    
    for g in groups:
        # Keep the one with the latest last_occurred_at
        docs = sorted(g['docs'], key=lambda x: x.get('last_occurred_at', x.get('timestamp')), reverse=True)
        winner = docs[0]
        losers = docs[1:]
        
        total_count = sum(d.get('count', 1) for d in docs)
        
        # Update the winner
        await db.errors.update_one(
            {"_id": winner["_id"]},
            {"$set": {"count": total_count}}
        )
        
        # Delete the losers
        for l in losers:
            await db.errors.delete_one({"_id": l["_id"]})
            
        print(f"Merged {len(docs)} documents for {g['_id']} into one with count {total_count}")

if __name__ == "__main__":
    asyncio.run(cleanup_duplicates())
