import asyncio
import uuid
import bcrypt

from app.database import connect_db
from app.config import get_settings
from app.timezone import now_ist

async def migrate():
    settings = get_settings()
    db = await connect_db(settings.MONGODB_URI, settings.MONGODB_DB_NAME, create_indexes=False)
    
    # 1. Create a default profile
    profile_id = str(uuid.uuid4())
    salt = bcrypt.gensalt()
    
    email = "admin@example.com"
    password = "password123"
    password_hash = bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    
    profile_doc = {
        "id": profile_id,
        "email": email,
        "name": "Admin Profile",
        "password_hash": password_hash,
        "created_at": now_ist(),
        "updated_at": now_ist()
    }
    
    existing = await db.profiles.find_one({})
    if existing:
        profile_id = existing["id"]
        print(f"Using existing profile: {existing['email']} (id: {profile_id})")
    else:
        await db.profiles.insert_one(profile_doc)
        print(f"Created default profile: {email} (id: {profile_id}) with password: {password}")
        
    # 2. Update all channels to have this profile_id
    result = await db.channels.update_many(
        {"profile_id": {"$exists": False}},
        {"$set": {"profile_id": profile_id}}
    )
    result2 = await db.channels.update_many(
        {"profile_id": "default"},
        {"$set": {"profile_id": profile_id}}
    )
    
    print(f"Migrated {result.modified_count + result2.modified_count} channels to profile {profile_id}")

if __name__ == "__main__":
    asyncio.run(migrate())
