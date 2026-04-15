import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import os

# Load .env if it exists
if os.path.exists(".env"):
    from pathlib import Path
    for line in Path(".env").read_text().splitlines():
        if line and not line.startswith("#"):
            key, val = line.split("=", 1)
            os.environ[key] = val.strip().strip('"')

# Assuming same env vars as the server
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "automation")

async def migrate():
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client[MONGODB_DB_NAME]
    
    print(f"Connecting to {MONGODB_DB_NAME}...")

    # 1. Identify all Instagram channels
    ig_channels = await db.channels.find({"platform": "instagram"}).to_list(length=None)
    ig_channel_ids = [c["channel_id"] for c in ig_channels]
    
    if not ig_channel_ids:
        print("No Instagram channels found. Nothing to migrate.")
        return

    # 2. Update status from 'scheduled' to 'queued' for Instagram videos
    # We only move those that don't have a media_id yet (meaning they haven't been published)
    vid_result = await db.videos.update_many(
        {
            "channel_id": {"$in": ig_channel_ids},
            "status": "scheduled",
            "instagram_media_id": None
        },
        {"$set": {"status": "queued"}}
    )
    print(f"Migrated {vid_result.modified_count} Instagram videos from 'scheduled' to 'queued'.")

    # 3. Add 'platform' field to schedule_queue entries
    # Update Instagram entries
    queue_ig = await db.schedule_queue.update_many(
        {"channel_id": {"$in": ig_channel_ids}},
        {"$set": {"platform": "instagram"}}
    )
    print(f"Updated {queue_ig.modified_count} schedule_queue entries with platform='instagram'.")

    # Update YouTube entries (remaining ones)
    yt_channels = await db.channels.find({"platform": "youtube"}).to_list(length=None)
    yt_channel_ids = [c["channel_id"] for c in yt_channels]
    
    queue_yt = await db.schedule_queue.update_many(
        {"channel_id": {"$in": yt_channel_ids}},
        {"$set": {"platform": "youtube"}}
    )
    print(f"Updated {queue_yt.modified_count} schedule_queue entries with platform='youtube'.")

    client.close()
    print("Migration complete.")

if __name__ == "__main__":
    asyncio.run(migrate())
