import asyncio
from motor.motor_asyncio import AsyncIOMotorClient

async def check_errors():
    client = AsyncIOMotorClient("mongodb://localhost:27017")
    db = client.automation_db
    errors = await db.errors.find().to_list(length=10)
    for err in errors:
        print(f"ID: {err.get('_id')}")
        print(f"Feature: {err.get('feature')}")
        print(f"Message: {err.get('message')}")
        print(f"Timestamp: {err.get('timestamp')}")
        print(f"Last Occurred At: {err.get('last_occurred_at')}")
        print(f"Count: {err.get('count')}")
        print("-" * 20)

if __name__ == "__main__":
    asyncio.run(check_errors())
