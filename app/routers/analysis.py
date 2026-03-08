"""Analysis router – run analysis updates and retrieve results."""

from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/analysis",
    tags=["analysis"],
    dependencies=[Depends(verify_api_key)],
)


def _get_services(channel_id: str):
    """Lazy import to avoid circular dependency."""
    from app.main import youtube_service_manager, gemini_service  # type: ignore[import]

    youtube_service = youtube_service_manager.get_service(channel_id) if youtube_service_manager else None
    return youtube_service, gemini_service


# ------------------------------------------------------------------
# POST /update  –  run the full analysis pipeline
# ------------------------------------------------------------------


@router.post("/update")
async def run_analysis_update(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Trigger a full analysis update for *channel_id*."""
    from app.services.analysis_engine import run_analysis

    youtube_service, gemini_service = _get_services(channel_id)

    if youtube_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No YouTube token for channel '{channel_id}'. Run: python generate_youtube_token.py {channel_id}",
        )
    if gemini_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    result = await run_analysis(
        channel_id, db, youtube_service, gemini_service
    )
    return result


# ------------------------------------------------------------------
# GET /latest  –  retrieve latest analysis
# ------------------------------------------------------------------


from app.models.analysis import Analysis

@router.get("/latest")
async def get_latest_analysis(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return the latest analysis document for *channel_id*,
    plus counts of videos ready / not yet ready for analysis."""
    from datetime import datetime, timedelta

    doc = await db.analysis.find_one({"channel_id": channel_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No analysis found for channel {channel_id}",
        )
    doc.pop("_id", None)

    already_analysed = set(doc.get("analysis_done_video_ids", []))
    three_days_ago = datetime.utcnow() - timedelta(days=3)

    unanalysed = await db.videos.find(
        {
            "channel_id": channel_id,
            "status": "published",
            "video_id": {"$nin": list(already_analysed)},
        },
        {"created_at": 1},
    ).to_list(length=None)

    ready_for_analysis = sum(
        1 for v in unanalysed
        if v.get("created_at", datetime.min) <= three_days_ago
    )
    not_ready_yet = len(unanalysed) - ready_for_analysis

    return {
        **doc,
        "analysis_status": {
            "ready_for_analysis": ready_for_analysis,
            "not_ready_yet": not_ready_yet,
        },
    }


# ------------------------------------------------------------------
# GET /history  –  retrieve analysis history
# ------------------------------------------------------------------

@router.get("/history")
async def get_analysis_history(
    channel_id: str,
    limit: int = 10,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return the history of analysis runs for *channel_id*."""
    cursor = db.analysis_history.find({"channel_id": channel_id}).sort("created_at", -1).limit(limit)
    history = await cursor.to_list(length=limit)
    for doc in history:
        doc.pop("_id", None)
    return history
