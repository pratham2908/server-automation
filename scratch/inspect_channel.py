import asyncio
import os

from motor.motor_asyncio import AsyncIOMotorClient


async def inspect():
    # Load .env if it exists
    if os.path.exists(".env"):
        from pathlib import Path

        for line in Path(".env").read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ[key] = val.strip().strip('"')

    uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    db_name = os.getenv("MONGODB_DB_NAME", "automation")

    client = AsyncIOMotorClient(uri)
    db = client[db_name]

    doc = await db.channels.find_one()
    if doc:
        import json

        from bson import ObjectId

        def clean(d):
            if isinstance(d, dict):
                return {k: clean(v) for k, v in d.items()}
            if isinstance(d, list):
                return [clean(v) for v in d]
            if isinstance(d, ObjectId):
                return str(d)
            if hasattr(d, "isoformat"):
                return d.isoformat()
            return d

        print(json.dumps(clean(doc), indent=2))
    else:
        print("No channels found")
    client.close()


if __name__ == "__main__":
    asyncio.run(inspect())
