"""Where a source app calls back when today's video is ready or has failed.

Outside the X-API-Key gate on purpose: the caller is another service holding a
one-time password we minted for this one callback, not a holder of our API key.
See ``services/today_callbacks.py`` for how the password is issued and checked,
and ``docs/today-callbacks.md`` for the contract the app implements.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.logger import get_logger
from app.models.today_callback import TodayCallbackBody, TodayCallbackReceipt
from app.services import today_callbacks
from app.services.auto_scheduler_cron import settle_today_callback, wake_auto_scheduler
from app.timezone import now_ist

logger = get_logger(__name__)

router = APIRouter(prefix=today_callbacks.CALLBACK_PATH, tags=["source-callbacks"])


@router.post("/{callback_id}", response_model=TodayCallbackReceipt)
async def receive_today_callback(
    callback_id: str,
    body: TodayCallbackBody,
    authorization: str | None = Header(None),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> TodayCallbackReceipt:
    if body.callback_id is not None and body.callback_id != callback_id:
        # Checked before claiming, so a mix-up does not use up the password.
        raise HTTPException(422, "callbackId does not match the callback URL")
    now = now_ist()
    outcome, record = await today_callbacks.claim(db, callback_id, today_callbacks.bearer_token(authorization), now)
    if outcome == "not_found":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown callback")
    if outcome == "unauthorized":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Missing or wrong callback token", headers={"WWW-Authenticate": "Bearer"}
        )
    if outcome == "already_received":
        raise HTTPException(status.HTTP_409_CONFLICT, "This callback was already delivered")
    if outcome == "gone":
        raise HTTPException(status.HTTP_410_GONE, "This callback is no longer wanted (its day or slot is over)")

    action = await settle_today_callback(db, record, body, now)
    await today_callbacks.record_outcome(db, callback_id, action)
    logger.info("Source callback %s for %s slot %s: %s", callback_id, record["channel_id"], record["slot"], action)
    wake_auto_scheduler()
    if action == "slot_closed":
        raise HTTPException(status.HTTP_410_GONE, "The slot this video was for has already closed")
    return TodayCallbackReceipt(received=True, action=action)
