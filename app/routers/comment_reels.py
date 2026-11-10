from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.config import get_settings
from app.database import get_db
from app.dependencies import verify_api_key_flexible as verify_api_key
from app.services.comment_reels import build_comment_reels
from app.services.comment_reels_email import format_comment_reels_email
from app.services.email_service import resolve_owner_email, send_email

router = APIRouter(prefix="/api/v1/comment-reels", tags=["comment-reels"], dependencies=[Depends(verify_api_key)])

# The two Instagram channels this was asked for: TryAlgoViz and The Map World.
DEFAULT_CHANNELS = ["TryAlgoViz", "DreamScenicAi"]


def _manager():
    import app.main as main_mod

    return main_mod.instagram_service_manager


async def _data(db: AsyncIOMotorDatabase, channels: list[str] | None, days: int):
    return await build_comment_reels(db, _manager(), channels or DEFAULT_CHANNELS, days, datetime.now(timezone.utc))


@router.get("/preview", response_class=HTMLResponse)
async def preview_comment_reels(
    channels: list[str] | None = Query(None, description="Channel ids; default TryAlgoViz and DreamScenicAi"),
    days: int = Query(3, ge=1, le=14),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """The email as HTML, without sending it."""
    return HTMLResponse(format_comment_reels_email(await _data(db, channels, days)).html)


@router.post("/send")
async def send_comment_reels(
    channels: list[str] | None = Query(None),
    days: int = Query(3, ge=1, le=14),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Email the reels that got comments in the last *days* days, with links that open them in Instagram."""
    data = await _data(db, channels, days)
    if not data["channels"]:
        raise HTTPException(404, detail="No reels with comments in that window")
    settings = get_settings()
    email = format_comment_reels_email(data)
    sent = await send_email(settings, await resolve_owner_email(db, settings), email.subject, email.text, email.html)
    return {"sent": sent, "reels": data["totals"]["reels"], "comments": data["totals"]["comments"]}
