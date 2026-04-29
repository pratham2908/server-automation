import asyncio

from motor.motor_asyncio import AsyncIOMotorClient


async def main():
    client = AsyncIOMotorClient(
        "mongodb+srv://admin_automation:BAsrZ7aE6290xO4M@automation0.lcyx8.mongodb.net/?retryWrites=true&w=majority&appName=automation0"
    )
    db = client.automation
    channels = await db.channels.find({}).to_list(length=None)
    for c in channels:
        if not c.get("platform"):
            platform = "instagram" if c.get("instagram_user_id") and not c.get("youtube_channel_id") else "youtube"
            await db.channels.update_one({"_id": c["_id"]}, {"$set": {"platform": platform}})
            print(f"Fixed {c.get('channel_id')} -> {platform}")


asyncio.run(main())
