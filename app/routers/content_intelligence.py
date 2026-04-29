"""Content Intelligence router -- scan, insights, and CRUD for video intelligence."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_channel_platform, get_db
from app.dependencies import verify_api_key
from app.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/content-intel/{channel_id}",
    tags=["content-intelligence"],
    dependencies=[Depends(verify_api_key)],
)


# ------------------------------------------------------------------
# POST /scan -- trigger a content intelligence scan
# ------------------------------------------------------------------


@router.post("/scan")
async def trigger_scan(
    channel_id: str,
    source: Optional[str] = Query(None, description="'competitor', 'own', or omit for both"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Scan competitor and/or own videos for content intelligence.

    Fetches video metadata, sends to Gemini for hook/CTA/structure
    extraction, and stores results persistently. Incremental — only
    processes videos not already in the collection.
    """
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found")

    import app.main as main_mod
    from app.services.content_intelligence import scan_competitor_videos, scan_own_videos

    if not main_mod.gemini_service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    platform = get_channel_platform(channel)
    results = []

    if source in (None, "competitor"):
        comp_result = await scan_competitor_videos(
            channel_id=channel_id,
            db=db,
            gemini_service=main_mod.gemini_service,
            youtube_manager=main_mod.youtube_service_manager,
            instagram_manager=main_mod.instagram_service_manager,
            platform=platform,
        )
        results.append(comp_result)

    if source in (None, "own"):
        own_result = await scan_own_videos(
            channel_id=channel_id,
            db=db,
            gemini_service=main_mod.gemini_service,
            platform=platform,
        )
        results.append(own_result)

    return {"ok": True, "channel_id": channel_id, "scan_results": results}


# ------------------------------------------------------------------
# POST /insights -- generate comparative insights
# ------------------------------------------------------------------


@router.post("/insights")
async def generate_insights_endpoint(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Generate comparative insights from stored video intelligence.

    Loads all stored intelligence for the channel, splits by source
    (own vs competitor), and sends to Gemini for pattern comparison.
    Returns structured insights with action items.
    """
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found")

    import app.main as main_mod
    from app.services.content_intelligence import generate_insights

    if not main_mod.gemini_service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    platform = get_channel_platform(channel)

    try:
        result = await generate_insights(
            channel_id=channel_id,
            db=db,
            gemini_service=main_mod.gemini_service,
            platform=platform,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return result


# ------------------------------------------------------------------
# GET /insights -- retrieve last generated insights
# ------------------------------------------------------------------


@router.get("/insights")
async def get_latest_insights(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return the most recently generated content insights for a channel."""
    doc = await db.content_insights.find_one({"channel_id": channel_id})
    if not doc:
        raise HTTPException(
            status_code=404,
            detail=f"No insights found for channel '{channel_id}'. Run POST /insights first.",
        )
    doc.pop("_id", None)
    return doc


# ------------------------------------------------------------------
# GET /videos -- list stored video intelligence entries
# ------------------------------------------------------------------


@router.get("/videos")
async def list_video_intelligence(
    channel_id: str,
    source: Optional[str] = Query(None, description="Filter by 'competitor' or 'own'"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List stored video intelligence entries for a channel."""
    query: dict = {"channel_id": channel_id}
    if source:
        query["source"] = source

    docs = (
        await db.video_intelligence.find(query).sort("views", -1).limit(limit).to_list(length=limit)
    )

    for d in docs:
        d.pop("_id", None)

    return docs


# ------------------------------------------------------------------
# GET /videos/{intel_id} -- get a single entry
# ------------------------------------------------------------------


@router.get("/videos/{intel_id}")
async def get_video_intelligence(
    channel_id: str,
    intel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get a single video intelligence entry."""
    doc = await db.video_intelligence.find_one(
        {"channel_id": channel_id, "intel_id": intel_id},
    )
    if not doc:
        raise HTTPException(status_code=404, detail=f"Intel entry '{intel_id}' not found")
    doc.pop("_id", None)
    return doc


# ------------------------------------------------------------------
# DELETE /scan -- clear all intelligence for a channel
# ------------------------------------------------------------------


@router.delete("/scan")
async def clear_intelligence(
    channel_id: str,
    source: Optional[str] = Query(
        None, description="Clear only 'competitor' or 'own', or omit for all"
    ),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Clear all video intelligence for a channel (or by source).

    This allows a full re-scan from scratch.
    """
    query: dict = {"channel_id": channel_id}
    if source:
        query["source"] = source

    result = await db.video_intelligence.delete_many(query)

    # Also clear insights if clearing all
    if not source:
        await db.content_insights.delete_many({"channel_id": channel_id})

    return {
        "ok": True,
        "channel_id": channel_id,
        "source_cleared": source or "all",
        "deleted_count": result.deleted_count,
    }
