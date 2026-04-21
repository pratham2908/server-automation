import asyncio
import json
import ssl
from motor.motor_asyncio import AsyncIOMotorClient
from app.config import get_settings
from app.services.youtube import YouTubeService

async def test_reach():
    settings = get_settings()
    # Force bypass SSL
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    client = AsyncIOMotorClient(
        settings.MONGODB_URI, 
        tls=True,
        tlsContext=ssl_context
    )
    db = client[settings.MONGODB_DB_NAME]
    
    # 1. Find a channel with tokens
    channel = await db.channels.find_one({"youtube_tokens.token": {"$exists": True}, "platform": "youtube"})
    if not channel:
        print("❌ No channel with YouTube tokens found.")
        return

    channel_id = channel["channel_id"]
    print(f"✅ Testing Reach for channel: {channel_id} ({channel.get('name')})")

    # 2. Find a video to test
    video = await db.videos.find_one({
        "channel_id": channel_id, 
        "youtube_video_id": {"$exists": True},
        "status": "published"
    })
    if not video:
        print("❌ No published YouTube videos found.")
        return

    youtube_video_id = video["youtube_video_id"]
    print(f"📹 Testing Video ID: {youtube_video_id} ({video.get('title')})")

    # 3. Initialize YouTubeService
    yt_service = YouTubeService(
        db=db,
        channel_id=channel_id,
        client_id=settings.YOUTUBE_CLIENT_ID,
        client_secret=settings.YOUTUBE_CLIENT_SECRET,
        tokens=channel["youtube_tokens"]
    )

    # 4. Attempt reach analytics
    print("📡 Querying YouTube Analytics...")
    try:
        reach = yt_service.get_video_reach_analytics(youtube_video_id)
        print(f"📊 Reach Results: {json.dumps(reach, indent=2)}")
    except Exception as e:
        print(f"❌ Failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_reach())
