"""Sync-analysis pipeline router -- config API and manual trigger."""

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger
from app.timezone import now_ist

logger = get_logger(__name__)

config_router = APIRouter(
    prefix="/api/v1/config/auto-pipeline",
    tags=["sync-analysis"],
    dependencies=[Depends(verify_api_key)],
)

trigger_router = APIRouter(
    prefix="/api/v1/sync-analysis",
    tags=["sync-analysis"],
    dependencies=[Depends(verify_api_key)],
)

_CONFIG_KEY = "sync_analysis_config"

_DEFAULTS = {
    "key": _CONFIG_KEY,
    "enabled": True,
    "interval_hours": 12,
    "analysis_threshold": 3,
}


# ------------------------------------------------------------------
# GET /config/auto-pipeline  --  read pipeline config
# ------------------------------------------------------------------


@config_router.get("/")
async def get_auto_pipeline_config(
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return the current auto-sync + analysis pipeline configuration."""
    doc = await db.config.find_one({"key": _CONFIG_KEY})
    if doc:
        doc.pop("_id", None)
        return doc
    return {**_DEFAULTS, "description": "Default -- not yet customised."}


# ------------------------------------------------------------------
# PUT /config/auto-pipeline  --  update pipeline config
# ------------------------------------------------------------------


class AutoPipelineConfigUpdate(BaseModel):
    enabled: Optional[bool] = Field(None, description="Enable or disable the auto-sync cron")
    interval_hours: Optional[int] = Field(None, ge=1, le=168, description="Sync interval in hours")
    analysis_threshold: Optional[int] = Field(None, ge=1, le=50, description="Min unanalyzed videos to trigger analysis")


@config_router.put("/")
async def update_auto_pipeline_config(
    body: AutoPipelineConfigUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Update the auto-sync + analysis pipeline configuration.

    Only provided fields are changed. The cron re-reads config each cycle.
    """
    updates: dict[str, Any] = {}
    for field, value in body.model_dump(exclude_none=True).items():
        updates[field] = value

    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    updates["updated_at"] = now_ist()

    await db.config.update_one(
        {"key": _CONFIG_KEY},
        {"$set": updates, "$setOnInsert": {"key": _CONFIG_KEY}},
        upsert=True,
    )

    doc = await db.config.find_one({"key": _CONFIG_KEY})
    doc.pop("_id", None)
    return {"ok": True, **doc}


# ------------------------------------------------------------------
# POST /sync-analysis/trigger  --  manually run one cycle
# ------------------------------------------------------------------


@trigger_router.post("/trigger")
async def trigger_sync_analysis(
    channel_id: Optional[str] = Query(None, description="Restrict to a single channel"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Manually trigger a sync + analysis cycle.

    If ``channel_id`` is provided, only that channel is processed.
    Otherwise all channels are processed.
    """
    import app.main as main_mod
    from app.services.sync_analysis_cron import run_sync_analysis_for_channel

    if not main_mod.gemini_service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    config = await db.config.find_one({"key": _CONFIG_KEY})
    threshold = (config or {}).get("analysis_threshold", _DEFAULTS["analysis_threshold"])

    if channel_id:
        channel = await db.channels.find_one({"channel_id": channel_id})
        if not channel:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Channel '{channel_id}' not found",
            )
        channels = [channel]
    else:
        channels = await db.channels.find().to_list(length=None)

    results = []
    for channel in channels:
        cid = channel.get("channel_id")
        if not cid:
            continue
        try:
            r = await run_sync_analysis_for_channel(
                channel_id=cid,
                channel=channel,
                db=db,
                youtube_service_manager=main_mod.youtube_service_manager,
                instagram_service_manager=main_mod.instagram_service_manager,
                gemini_service=main_mod.gemini_service,
                analysis_threshold=threshold,
            )
            results.append(r)
        except Exception as exc:
            results.append({"channel_id": cid, "error": str(exc)})

    return {"ok": True, "channels_processed": len(results), "results": results}
