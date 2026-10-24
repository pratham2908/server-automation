"""Settings for Meta's Instagram webhook: the token Meta echoes when the callback is set up, and the
app secret every delivery is signed with.

Kept in the ``config`` collection like the other OAuth settings, so they're set from the dashboard
rather than by editing the server's environment. The app secret is the *Instagram* app's (Instagram
Login), which is not the Facebook app secret the Facebook Login flow uses.
"""

from __future__ import annotations

import secrets
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

CONFIG_KEY = "instagram_webhook"
WEBHOOK_PATH = "/api/v1/webhooks/instagram"


async def load(db: AsyncIOMotorDatabase) -> dict[str, Any]:
    return dict(await db.config.find_one({"key": CONFIG_KEY}) or {})


async def verify_token(db: AsyncIOMotorDatabase) -> str:
    """The verify token, minted on first use. Meta only needs it to match what's pasted in its dashboard."""
    token = str((await load(db)).get("verify_token") or "")
    if not token:
        token = secrets.token_urlsafe(24)
        await db.config.update_one({"key": CONFIG_KEY}, {"$set": {"verify_token": token}}, upsert=True)
    return token


async def app_secret(db: AsyncIOMotorDatabase) -> str | None:
    return (await load(db)).get("app_secret") or None


async def set_app_secret(db: AsyncIOMotorDatabase, secret: str) -> None:
    await db.config.update_one({"key": CONFIG_KEY}, {"$set": {"app_secret": secret}}, upsert=True)
