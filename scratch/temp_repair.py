import asyncio
from app.config import get_settings
from app.database import connect_db, close_db
from app.timezone import now_ist

async def repair():
    settings = get_settings()
    db = await connect_db(settings.MONGODB_URI, settings.MONGODB_DB_NAME, create_indexes=False)
    vid2 = '19358af6-0c6f-4234-a69c-e3e6973a6a3a'
    await db.videos.update_one({'video_id': vid2}, {'$set': {'status': 'ready', 'updated_at': now_ist()}})
    print(f'Repaired {vid2}: Reset status to ready')
    await close_db()

if __name__ == '__main__':
    asyncio.run(repair())
