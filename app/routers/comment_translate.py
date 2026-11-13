"""Translate a comment into English, for the Comments → Insights page."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger
from app.services import comment_translate as ct
from app.timezone import now_ist

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/comments", tags=["comment-translate"], dependencies=[Depends(verify_api_key)])


class TranslateIn(BaseModel):
    text: str = Field(min_length=1, max_length=ct.MAX_TEXT)


@router.post("/translate")
async def translate(body: TranslateIn, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    """English for one comment. Kept by text, so reading the same comment again costs nothing."""
    key = hashlib.sha256(body.text.encode()).hexdigest()
    cached = await db.comment_translations.find_one({"_id": key})
    if cached:
        return {k: cached[k] for k in ("language", "english", "already_english")}

    import app.main as main_mod

    gemini = getattr(main_mod, "gemini_service", None)
    if gemini is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="The AI service is unavailable")
    try:
        result = ct.parse_translation(await gemini.translate_comment(ct.build_prompt(body.text)), body.text)
    except Exception as exc:  # noqa: BLE001 - shown to the owner as a failed try; nothing is cached
        logger.warning("Translating a comment failed: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"The AI call failed: {exc}") from exc
    await db.comment_translations.replace_one({"_id": key}, {**result, "created_at": now_ist()}, upsert=True)
    return result
