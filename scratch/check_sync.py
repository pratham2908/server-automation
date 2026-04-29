import asyncio
import os

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient


async def check_sync_status():
    load_dotenv(".env")
    uri = os.getenv("MONGODB_URI")
    db_name = os.getenv("MONGODB_DB_NAME")

    client = AsyncIOMotorClient(uri)
    db = client[db_name]

    print("--- Config ---")
    config = await db.config.find_one({"key": "sync_analysis_config"})
    print(config)

    print("\n--- Channels Task Status ---")
    channels = await db.channels.find().to_list(length=None)
    for c in channels:
        print(f"Channel: {c.get('channel_id')} ({c.get('platform', 'youtube')})")
        print(f"  Last Tasks: {c.get('last_tasks')}")

    print("\n--- Unanalyzed Videos Count (the way cron does it) ---")
    for c in channels:
        cid = c.get("channel_id")
        count = await db.videos.count_documents(
            {
                "channel_id": cid,
                "status": "published",
                "verification_status": {"$ne": "unverified"},
                "performance": None,
            }
        )
        print(f"Channel {cid}: {count} videos meet criteria")

    print("\n--- Total Published but Unanalyzed Videos ---")
    for c in channels:
        cid = c.get("channel_id")
        count = await db.videos.count_documents(
            {
                "channel_id": cid,
                "status": "published",
                "performance": None,
            }
        )
        print(f"Channel {cid}: {count} total published but unanalyzed")

    client.close()


if __name__ == "__main__":
    asyncio.run(check_sync_status())
