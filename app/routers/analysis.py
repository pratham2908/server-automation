"""Analysis router – run analysis updates and retrieve results."""

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/analysis",
    tags=["analysis"],
    dependencies=[Depends(verify_api_key)],
)


def _get_services():
    """Lazy import to avoid circular dependency – replaced at startup."""
    from app.main import youtube_service, gemini_service  # type: ignore[import]

    return youtube_service, gemini_service


# ------------------------------------------------------------------
# POST /update  –  run the full analysis pipeline
# ------------------------------------------------------------------


@router.post("/update")
async def run_analysis_update(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Trigger a full analysis update for *channel_id*.

    This is a heavy endpoint — it fetches YouTube stats, calls Gemini,
    and regenerates to-do videos.
    """
    from app.services.analysis_engine import run_analysis

    youtube_service, gemini_service = _get_services()

    if youtube_service is None or gemini_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="YouTube or Gemini service not initialised",
        )

    result = await run_analysis(
        channel_id, db, youtube_service, gemini_service
    )
    return result


# ------------------------------------------------------------------
# GET /latest  –  retrieve latest analysis
# ------------------------------------------------------------------


@router.get("/latest")
async def get_latest_analysis(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return the latest analysis document for *channel_id*."""
    doc = await db.analysis.find_one({"channel_id": channel_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No analysis found for channel {channel_id}",
        )
    doc.pop("_id", None)
    return doc
