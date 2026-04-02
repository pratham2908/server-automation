"""Pre-publish scorecard router -- unified readiness assessment."""

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/videos/{video_id}",
    tags=["scorecard"],
    dependencies=[Depends(verify_api_key)],
)


@router.post("/scorecard")
async def create_scorecard(
    channel_id: str,
    video_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Generate a pre-publish readiness scorecard for a video.

    Aggregates all available pre-publish signals:

    - **Retention analysis** (if a prediction exists for this video)
    - **Thumbnail analysis** (if a video-linked analysis exists)
    - **Title & description** quality (evaluated by Gemini on the spot)
    - **Content param alignment** (compared against channel's best-performing patterns)
    - **Posting time** alignment (compared against best posting times from analysis)

    Returns a unified scorecard with an overall score (0-100), per-dimension
    breakdowns, top issues to fix, and a natural language publish recommendation.
    """
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found")

    video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if not video:
        raise HTTPException(status_code=404, detail=f"Video '{video_id}' not found")

    import app.main as main_mod

    if not main_mod.gemini_service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    platform = channel.get("platform", "youtube")

    from app.services.scorecard import generate_scorecard

    try:
        result = await generate_scorecard(
            channel_id=channel_id,
            video_id=video_id,
            db=db,
            gemini_service=main_mod.gemini_service,
            platform=platform,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Scorecard generation failed for video '%s': %s", video_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Scorecard generation failed: {exc}",
        )

    return result
