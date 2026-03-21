"""Retention analysis router — video retention prediction endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger
from app.timezone import now_ist

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/retention-analysis",
    tags=["retention-analysis"],
    dependencies=[Depends(verify_api_key)],
)


# ------------------------------------------------------------------
# GET /history — list retention analyses for a channel
# ------------------------------------------------------------------


@router.get("/history")
async def list_retention_analyses(
    channel_id: str,
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status: pending, analyzing, completed, failed"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List retention analyses for a channel, newest first."""
    query: dict = {"channel_id": channel_id}
    if status_filter:
        query["status"] = status_filter

    cursor = db.retention_analysis.find(query).sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)

    for d in docs:
        d["_id"] = str(d["_id"])

    return docs


# ------------------------------------------------------------------
# GET /{video_id} — get retention analysis for a specific video
# ------------------------------------------------------------------


@router.get("/{video_id}")
async def get_retention_analysis(
    channel_id: str,
    video_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get the retention analysis for a specific video.

    If actual metrics have been backfilled, includes a ``comparison``
    sub-object with deviation and accuracy metrics.
    """
    doc = await db.retention_analysis.find_one(
        {"channel_id": channel_id, "video_id": video_id}
    )
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No retention analysis found for video {video_id}",
        )

    doc["_id"] = str(doc["_id"])

    from app.services.retention_analysis import compute_comparison
    doc["comparison"] = compute_comparison(doc)

    return doc


# ------------------------------------------------------------------
# POST /{video_id}/trigger — manually trigger retention analysis
# ------------------------------------------------------------------


@router.post("/{video_id}/trigger")
async def trigger_retention_analysis(
    channel_id: str,
    video_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Manually trigger (or re-trigger) retention analysis for a video.

    The video must have an R2 file (``r2_object_key``).
    """
    video = await db.videos.find_one(
        {"channel_id": channel_id, "video_id": video_id}
    )
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video {video_id} not found",
        )
    allowed_statuses = {"ready", "scheduled", "published"}
    video_status = video.get("status", "")
    if video_status not in allowed_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Video must be in ready/scheduled/published status to analyze (current: '{video_status}')",
        )
    if not video.get("r2_object_key"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video has no R2 file — cannot run retention analysis",
        )

    import asyncio
    from app.main import r2_service, gemini_service  # type: ignore[import]
    from app.services.retention_analysis import run_retention_analysis

    if not r2_service or not gemini_service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Services not initialised",
        )

    asyncio.create_task(
        run_retention_analysis(channel_id, video_id, db, r2_service, gemini_service)
    )

    return {
        "ok": True,
        "video_id": video_id,
        "message": "Retention analysis triggered — poll GET /{video_id} for status",
    }


# ------------------------------------------------------------------
# DELETE /{video_id} — delete a retention analysis
# ------------------------------------------------------------------


@router.delete("/{video_id}")
async def delete_retention_analysis(
    channel_id: str,
    video_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Delete the retention analysis for a specific video."""
    result = await db.retention_analysis.delete_one(
        {"channel_id": channel_id, "video_id": video_id}
    )
    if result.deleted_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No retention analysis found for video {video_id}",
        )
    return {"ok": True, "video_id": video_id, "deleted": True}
