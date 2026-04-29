import asyncio
import os
import sys
sys.path.append(os.getcwd())

from motor.motor_asyncio import AsyncIOMotorClient
from app.config import get_settings

async def find_duplicates():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DB_NAME]
    
    pipeline = [
        {"$match": {"resolved": False}},
        {"$group": {
            "_id": {"feature": "$feature", "message": "$message"},
            "count": {"$sum": 1},
            "docs": {"$push": "$_id"}
        }},
        {"$match": {"count": {"$gt": 1}}}
    ]
    
    duplicates = await db.errors.aggregate(pipeline).to_list(length=None)
    if not duplicates:
        print("No duplicates found with exact same feature and message.")
    else:
        for d in duplicates:
            print(f"Duplicate found: {d['_id']}")
            print(f"Number of documents: {d['count']}")
            print(f"IDs: {d['docs']}")
            print("-" * 20)

if __name__ == "__main__":
    asyncio.run(find_duplicates())
