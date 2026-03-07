"""One-time backfill: set `published_at` for every published video.

For videos with status "published", `published_at` is set to the existing
`created_at` value (which was originally populated from YouTube's publishedAt
during sync).  Non-published videos are left with `published_at = null`.

Usage:
    python backfill_published_at.py
"""

import asyncio
import os

import certifi
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "youtube_automation")


async def main():
    client = AsyncIOMotorClient(MONGODB_URI, tlsCAFile=certifi.where())
    db = client[MONGODB_DB_NAME]

    cursor = db.videos.find(
        {"status": "published", "published_at": {"$exists": False}},
    )

    updated = 0
    async for video in cursor:
        published_at = video.get("created_at")
        if published_at is None:
            continue
        await db.videos.update_one(
            {"_id": video["_id"]},
            {"$set": {"published_at": published_at}},
        )
        updated += 1

    # Videos that are NOT published get an explicit null so the field exists.
    result = await db.videos.update_many(
        {"status": {"$ne": "published"}, "published_at": {"$exists": False}},
        {"$set": {"published_at": None}},
    )

    print(f"Set published_at on {updated} published video(s).")
    print(f"Set published_at=null on {result.modified_count} non-published video(s).")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
