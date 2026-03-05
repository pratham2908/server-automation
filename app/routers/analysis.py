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
    """Trigger a full analysis update for *channel_id*."""
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


# ------------------------------------------------------------------
# POST /updateToDoList  –  generate n new videos based on latest analysis
# ------------------------------------------------------------------

class TodoGenerateRequest(BaseModel):
    n: int = Field(gt=0, description="The number of videos to generate")


@router.post("/updateToDoList")
async def generate_todos(
    channel_id: str,
    body: TodoGenerateRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Generate `n` new to-do videos for *channel_id*."""
    from app.services.todo_engine import generate_todo_videos

    _, gemini_service = _get_services()

    if gemini_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    await generate_todo_videos(
        channel_id=channel_id,
        target_count=body.n,
        db=db,
        gemini_service=gemini_service,
    )

    return {
        "ok": True,
        "message": f"Successfully generated {body.n} new videos for the to-do list.",
    }
