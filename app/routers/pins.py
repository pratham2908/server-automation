from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.config import get_settings
from app.database import get_db
from app.dependencies import verify_api_key_flexible as verify_api_key
from app.services.pin_email import send_pending_pins_email
from app.services.pin_tracker import pins_overview, set_pin_status

router = APIRouter(prefix="/api/v1/pins", tags=["pins"], dependencies=[Depends(verify_api_key)])


def _manager():
    import app.main as main_mod

    return main_mod.instagram_service_manager


class PinStatusBody(BaseModel):
    # null un-ticks the reel, putting it back on the pending list.
    status: Literal["done", "skipped"] | None


@router.get("")
async def list_pins(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Per channel: reels whose first comment still needs pinning, and the ones already finished."""
    return await pins_overview(db, _manager())


@router.patch("/{video_id}")
async def update_pin(video_id: str, body: PinStatusBody, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Tick a reel as pinned (or skipped), or un-tick it."""
    if not await set_pin_status(db, video_id, body.status):
        raise HTTPException(404, detail="No tracked reel with that id")
    return {"video_id": video_id, "status": body.status}


@router.post("/email")
async def email_pins(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Send the pending-pins email now, even if nothing is new."""
    return {"outcome": await send_pending_pins_email(db, _manager(), get_settings(), force=True)}
