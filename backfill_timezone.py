"""One-time backfill: convert all UTC timestamps to IST (GMT+5:30).

Existing timestamps in the DB were stored as naive UTC datetimes.
This script adds 5 hours 30 minutes to every timestamp field across
all collections so they represent IST.

Collections and fields updated:
  - videos:           created_at, updated_at, published_at, scheduled_at
  - channels:         created_at, updated_at
  - categories:       created_at, updated_at
  - analysis:         created_at, updated_at
  - analysis_history: created_at
  - posting_queue:    added_at
  - schedule_queue:   added_at, scheduled_at

Usage:
    python backfill_timezone.py
"""

import asyncio
import os
from datetime import timedelta

import certifi
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "youtube_automation")

IST_OFFSET = timedelta(hours=5, minutes=30)

COLLECTIONS_AND_FIELDS = {
    "videos": ["created_at", "updated_at", "published_at", "scheduled_at"],
    "channels": ["created_at", "updated_at"],
    "categories": ["created_at", "updated_at"],
    "analysis": ["created_at", "updated_at"],
    "analysis_history": ["created_at"],
    "posting_queue": ["added_at"],
    "schedule_queue": ["added_at", "scheduled_at"],
}


async def main():
    client = AsyncIOMotorClient(MONGODB_URI, tlsCAFile=certifi.where())
    db = client[MONGODB_DB_NAME]

    for collection_name, fields in COLLECTIONS_AND_FIELDS.items():
        collection = db[collection_name]
        total_docs = await collection.count_documents({})
        print(f"\n--- {collection_name} ({total_docs} docs) ---")

        updated = 0
        async for doc in collection.find({}):
            set_fields = {}
            for field in fields:
                val = doc.get(field)
                if val is None:
                    continue
                set_fields[field] = val + IST_OFFSET

            if set_fields:
                await collection.update_one(
                    {"_id": doc["_id"]},
                    {"$set": set_fields},
                )
                updated += 1

        print(f"  Updated {updated}/{total_docs} documents")

    print("\nBackfill complete — all timestamps are now IST (GMT+5:30).")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
