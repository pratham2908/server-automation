import asyncio
import os
import sys
sys.path.append(os.getcwd())

from motor.motor_asyncio import AsyncIOMotorClient
from app.config import get_settings

async def clear_meta_errors():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DB_NAME]
    
    result = await db.errors.delete_many({
        "feature": {"$regex": "api/errors", "$options": "i"}
    })
    print(f"Deleted {result.deleted_count} meta-endpoint error entries.")

if __name__ == "__main__":
    asyncio.run(clear_meta_errors())
