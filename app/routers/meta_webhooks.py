"""Meta's Instagram webhook: new comments arrive here within seconds of being written.

Outside the X-API-Key gate on purpose — Meta can't hold our key. Instead the setup handshake is checked
against our verify token, and every delivery against its HMAC signature made with the Instagram app
secret; an unsigned or mis-signed delivery is refused before anything is read from it.
"""

from __future__ import annotations

import json
import random

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.logger import get_logger
from app.services import meta_webhook_config
from app.services.comment_dm import parse_comment_webhook, verify_signature
from app.services.comment_dm_store import QUEUED, channel_for_webhook, enqueue
from app.timezone import now_ist

logger = get_logger(__name__)

router = APIRouter(prefix=meta_webhook_config.WEBHOOK_PATH, tags=["meta-webhooks"])


@router.get("", response_class=PlainTextResponse)
async def verify_subscription(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> str:
    """Meta's one-time check when the callback URL is saved: echo the challenge if the token matches."""
    if mode != "subscribe" or token != await meta_webhook_config.verify_token(db):
        raise HTTPException(403, "Verification failed")
    return challenge


@router.post("")
async def receive(request: Request, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, int]:
    secret = await meta_webhook_config.app_secret(db)
    if not secret:
        # Fail closed: without the secret nothing proves the delivery came from Meta. Meta retries, and
        # the poll still answers comments on channels that have a Facebook token.
        logger.error("[CommentDM] Webhook delivery refused: Instagram app secret not configured")
        raise HTTPException(503, "Webhook secret not configured")
    body = await request.body()
    if not verify_signature(secret, body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(403, "Bad signature")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(400, "Body is not JSON") from exc
    events = parse_comment_webhook(payload if isinstance(payload, dict) else {})
    now = now_ist()
    rng = random.Random()
    queued = 0
    for event in events:
        channel = await channel_for_webhook(db, event.account_id, event.media_id)
        if channel is None:
            logger.info("[CommentDM] Webhook comment %s on unknown media %s", event.comment_id, event.media_id)
            continue
        queued += await enqueue(db, channel, event, source="webhook", now=now, rng=rng) == QUEUED
    return {"comments": len(events), "queued": queued}
