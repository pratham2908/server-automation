"""Replies telling the people who asked for a city that its meet reel is out.

See ``app.services.city_requesters`` (rules) and ``app.services.city_requester_store`` (how it runs).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import verify_api_key
from app.services import city_requester_store as store
from app.timezone import now_ist

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/city-requesters",
    tags=["city-requesters"],
    dependencies=[Depends(verify_api_key)],
)


class SettingIn(BaseModel):
    enabled: bool


async def _channel(db: AsyncIOMotorDatabase, channel_id: str) -> None:
    if not await db.channels.find_one({"channel_id": channel_id}, {"_id": 1}):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Channel not found")


@router.get("")
async def overview(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    """The switch, and every run so far (newest first)."""
    await _channel(db, channel_id)
    return {"setting": await store.setting(db, channel_id), "jobs": await store.jobs(db, channel_id)}


@router.put("/settings")
async def set_setting(channel_id: str, body: SettingIn, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    """On: meet reels that go up from now on get their requesters told. Earlier ones never are."""
    await _channel(db, channel_id)
    return await store.set_enabled(db, channel_id, body.enabled, now_ist())


@router.get("/preview")
async def preview(
    channel_id: str,
    city: str = Query(..., min_length=1, max_length=80),
    media_id: str = Query("", description="The meet reel itself, whose own comments don't count"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """Dry run: who asked for the city and what each would be sent. Sends and reads nothing on Instagram,
    so people already told by hand still show here; a run skips them after reading their thread."""
    await _channel(db, channel_id)
    key, label, people = await store.requesters(db, channel_id, city, media_id, now_ist())
    told = {r["username"]: r["status"] for r in await store.replies_for(db, channel_id, label)}
    rows = store.preview(people, label)
    for row in rows:
        row["recorded"] = told.get(row["username"])
    return {"city": label, "city_key": key, "people": len(rows), "would_reply": rows}


@router.get("/replies")
async def replies(
    channel_id: str, city: str = Query(..., min_length=1, max_length=80), db: AsyncIOMotorDatabase = Depends(get_db)
) -> list[dict[str, Any]]:
    """Everyone handled for a city: sent (with the reply id), already told, comment gone, or failed."""
    await _channel(db, channel_id)
    return await store.replies_for(db, channel_id, city)


@router.post("/jobs/{media_id}/retry")
async def retry(channel_id: str, media_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    """Pick a stopped run up again on the next pass; each person is checked before anything is sent."""
    try:
        return await store.retry(db, channel_id, media_id, now_ist())
    except store.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
