from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.config import get_settings
from app.database import get_db
from app.dependencies import verify_api_key_flexible as verify_api_key
from app.services.daily_digest import build_digest
from app.services.daily_digest_cron import send_digest
from app.services.daily_digest_email import format_digest_email
from app.timezone import now_ist

router = APIRouter(prefix="/api/v1/digest", tags=["digest"], dependencies=[Depends(verify_api_key)])


def _day(value: str | None) -> date:
    if not value:
        return now_ist().date() - timedelta(days=1)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD") from None


@router.get("/preview", response_class=HTMLResponse)
async def preview_digest(
    date_: str | None = Query(None, alias="date", description="IST date, default yesterday"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """The daily summary email as HTML, for looking at it without sending anything."""
    digest = await build_digest(db, _day(date_))
    return HTMLResponse(format_digest_email(digest, get_settings().ANALYZER_PUBLIC_URL).html)


@router.post("/send")
async def send_digest_now(
    date_: str | None = Query(None, alias="date", description="IST date, default yesterday"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Send the summary now, whether or not the daily one has gone out."""
    return {"outcome": await send_digest(db, get_settings(), _day(date_))}
