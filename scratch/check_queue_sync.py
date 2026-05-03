import asyncio
import sys
import os

# Add the parent directory to sys.path so we can import app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings
from app.database import connect_db, close_db

async def check_inconsistencies():
    settings = get_settings()
    db = await connect_db(settings.MONGODB_URI, settings.MONGODB_DB_NAME, create_indexes=False)
    
    print(f"Connected to DB: {settings.MONGODB_DB_NAME}")
    
    # 1. Find all videos with status 'queued'
    queued_videos = await db.videos.find({"status": "queued"}).to_list(length=None)
    print(f"Found {len(queued_videos)} videos with status 'queued' in the 'videos' collection.")
    
    inconsistent_count = 0
    inconsistent_videos = []

    for video in queued_videos:
        video_id = video.get("video_id")
        channel_id = video.get("channel_id")
        
        # Check if this video exists in schedule_queue
        queue_entry = await db.schedule_queue.find_one({"video_id": video_id, "channel_id": channel_id})
        
        if not queue_entry:
            inconsistent_count += 1
            inconsistent_videos.append({
                "video_id": video_id,
                "channel_id": channel_id,
                "title": video.get("title", "No Title")
            })
            print(f"MISSING: Video '{video_id}' (Channel: '{channel_id}') is 'queued' but NOT in 'schedule_queue'")

    print("\n--- Summary ---")
    if inconsistent_count == 0:
        print("✅ No inconsistencies found. All 'queued' videos are in the schedule_queue.")
    else:
        print(f"❌ Found {inconsistent_count} inconsistent videos.")
        for v in inconsistent_videos:
            print(f"  - {v['title']} ({v['video_id']}) [Channel: {v['channel_id']}]")

    await close_db()

if __name__ == "__main__":
    asyncio.run(check_inconsistencies())
