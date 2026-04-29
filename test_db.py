import asyncio

from motor.motor_asyncio import AsyncIOMotorClient


async def main():
    client = AsyncIOMotorClient(
        "mongodb+srv://admin_automation:BAsrZ7aE6290xO4M@automation0.lcyx8.mongodb.net/?retryWrites=true&w=majority&appName=automation0"
    )
    db = client.automation
    async for c in db.channels.find({}):
        print(c.get("channel_id"), c.get("platform"))


asyncio.run(main())
