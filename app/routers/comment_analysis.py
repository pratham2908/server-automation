"""Comment analysis router -- read endpoints, manual trigger, and config."""

from typing import Any, Optional

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger
from app.timezone import now_ist

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/comment-analysis",
    tags=["comment-analysis"],
    dependencies=[Depends(verify_api_key)],
)

# A second router for global config (not channel-scoped)
config_router = APIRouter(
    prefix="/api/v1/comment-analysis/config",
    tags=["comment-analysis"],
    dependencies=[Depends(verify_api_key)],
)


# ------------------------------------------------------------------
# GET /config  –  read current comment analysis schedule config
# ------------------------------------------------------------------


class CommentAnalysisConfigUpdate(BaseModel):
    analysis_hour: int = Field(..., ge=0, le=23, description="Hour of day (0-23) in IST when the cron runs")


@config_router.get("/")
async def get_comment_analysis_config(
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return the current comment analysis schedule configuration."""
    doc = await db.config.find_one({"key": "comment_analysis_config"})
    if doc:
        doc.pop("_id", None)
        return doc

    return {
        "key": "comment_analysis_config",
        "analysis_hour": 3,
        "description": "Default — not yet customised. Cron runs daily at 03:00 IST.",
    }


# ------------------------------------------------------------------
# PUT /config  –  update comment analysis schedule config
# ------------------------------------------------------------------


@config_router.put("/")
async def update_comment_analysis_config(
    body: CommentAnalysisConfigUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Update the comment analysis schedule.

    The cron reads this value before each sleep cycle, so changes
    take effect from the next scheduled run (no server restart needed).
    """
    await db.config.update_one(
        {"key": "comment_analysis_config"},
        {"$set": {
            "key": "comment_analysis_config",
            "analysis_hour": body.analysis_hour,
            "updated_at": now_ist(),
        }},
        upsert=True,
    )

    logger.info("Comment analysis config updated: analysis_hour=%d", body.analysis_hour)

    return {
        "ok": True,
        "analysis_hour": body.analysis_hour,
        "message": f"Comment analysis cron will run daily at {body.analysis_hour:02d}:00 IST",
    }


# ------------------------------------------------------------------
# POST /trigger  –  manually run one cron cycle for this channel
# ------------------------------------------------------------------


@router.post("/trigger")
async def trigger_comment_analysis(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Manually trigger a comment-analysis cycle for *channel_id*.

    Runs the same logic as the 24-hour cron job but on-demand.
    """
    from app.services.comment_analysis_engine import run_cron_cycle

    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    platform = channel.get("platform", "youtube")

    from app.main import youtube_service_manager, instagram_service_manager, gemini_service  # type: ignore[import]

    if gemini_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    stats = await run_cron_cycle(
        db=db,
        youtube_service_manager=youtube_service_manager,
        instagram_service_manager=instagram_service_manager,
        gemini_service=gemini_service,
        channel_id=channel_id,
        platform=platform,
    )

    return {"ok": True, "channel_id": channel_id, **stats}


# ------------------------------------------------------------------
# GET /history  –  list all analyses for a channel
# ------------------------------------------------------------------


@router.get("/history")
async def get_comment_analysis_history(
    channel_id: str,
    source: Optional[str] = Query(None, description="Filter by 'own' or 'competitor'"),
    platform: Optional[str] = Query(None, description="Filter by 'youtube' or 'instagram'"),
    limit: Optional[int] = Query(None, description="Max results"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return comment analyses for *channel_id* with optional filters."""
    query: dict[str, Any] = {"channel_id": channel_id}
    if source:
        query["source"] = source
    if platform:
        query["platform"] = platform

    cursor = db.comment_analysis.find(query).sort("analyzed_at", -1)
    if limit is not None:
        cursor = cursor.limit(limit)

    results = await cursor.to_list(length=limit if limit is not None else None)
    for doc in results:
        doc["_id"] = str(doc["_id"])
    return results


# ------------------------------------------------------------------
# GET /aggregate  –  combined insights across all analyses
# ------------------------------------------------------------------


@router.get("/aggregate")
async def aggregate_comment_insights(
    channel_id: str,
    source: Optional[str] = Query(None, description="Filter by 'own' or 'competitor'"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Aggregate all comment analyses into a channel-level intelligence report."""
    from app.services.comment_analysis_engine import aggregate_comment_analyses

    return await aggregate_comment_analyses(db, channel_id, source_filter=source)


# ------------------------------------------------------------------
# GET /{analysis_id}  –  single analysis by MongoDB _id
# ------------------------------------------------------------------


@router.get("/{analysis_id}")
async def get_comment_analysis(
    channel_id: str,
    analysis_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return a specific comment analysis document."""
    try:
        oid = ObjectId(analysis_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid analysis_id: {analysis_id}",
        )

    doc = await db.comment_analysis.find_one({"_id": oid, "channel_id": channel_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No comment analysis found with id '{analysis_id}'",
        )

    doc["_id"] = str(doc["_id"])
    return doc


# ------------------------------------------------------------------
# DELETE /{analysis_id}  –  delete one analysis
# ------------------------------------------------------------------


@router.delete("/{analysis_id}")
async def delete_comment_analysis(
    channel_id: str,
    analysis_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Delete a specific comment analysis document."""
    try:
        oid = ObjectId(analysis_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid analysis_id: {analysis_id}",
        )

    result = await db.comment_analysis.delete_one({"_id": oid, "channel_id": channel_id})
    if result.deleted_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No comment analysis found with id '{analysis_id}'",
        )

    return {"ok": True, "deleted": True, "analysis_id": analysis_id}


# ------------------------------------------------------------------
# DELETE /  –  delete all comment analyses for a channel
# ------------------------------------------------------------------


@router.delete("/")
async def delete_all_comment_analyses(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Delete all comment analysis documents for *channel_id*."""
    result = await db.comment_analysis.delete_many({"channel_id": channel_id})

    logger.info(
        "Deleted %d comment analyses for channel '%s'",
        result.deleted_count, channel_id,
    )

    return {
        "ok": True,
        "channel_id": channel_id,
        "deleted_count": result.deleted_count,
    }


