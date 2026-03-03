"""Videos router – list, status update, and queue addition."""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
from app.models.video import VideoCreate, VideoStatus, VideoStatusUpdate
from app.services.r2 import R2Service

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/videos",
    tags=["videos"],
    dependencies=[Depends(verify_api_key)],
)


def _get_r2() -> R2Service:
    """Lazy import to avoid circular dependency – replaced at startup."""
    from app.main import r2_service  # type: ignore[import]

    if r2_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="R2 service not initialised",
        )
    return r2_service


# ------------------------------------------------------------------
# GET /  –  video list (with optional suggest_n)
# ------------------------------------------------------------------


@router.get("/")
async def list_videos(
    channel_id: str,
    status_filter: Optional[str] = None,
    suggest_n: Optional[int] = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return videos for *channel_id*.

    Query params
    ------------
    status : ``done`` | ``todo`` | ``all`` (default ``all``)
    suggest_n : if provided, mark the top *n* to-do videos as suggested.
    """
    query: dict = {"channel_id": channel_id}
    if status_filter and status_filter != "all":
        query["status"] = status_filter

    # If suggest_n is requested, pick top-N to-do videos (ordered by
    # category score) and flag them.
    if suggest_n and suggest_n > 0:
        # Reset previous suggestions for this channel.
        await db.videos.update_many(
            {"channel_id": channel_id, "suggested": True},
            {"$set": {"suggested": False, "updated_at": datetime.utcnow()}},
        )

        # Fetch active categories sorted by score to determine priority.
        categories = (
            await db.categories.find(
                {"channel_id": channel_id, "status": "active"}
            )
            .sort("score", -1)
            .to_list(length=None)
        )
        cat_order = {c["name"]: idx for idx, c in enumerate(categories)}

        todo_videos = await db.videos.find(
            {"channel_id": channel_id, "status": "todo"}
        ).to_list(length=None)

        # Sort by category score (best first).
        todo_videos.sort(key=lambda v: cat_order.get(v.get("category", ""), 9999))

        for v in todo_videos[:suggest_n]:
            await db.videos.update_one(
                {"_id": v["_id"]},
                {"$set": {"suggested": True, "updated_at": datetime.utcnow()}},
            )

    videos = await db.videos.find(query).to_list(length=None)

    # Strip Mongo _id for JSON serialisation.
    for v in videos:
        v.pop("_id", None)

    return videos


# ------------------------------------------------------------------
# PATCH /{video_id}/status  –  mark done / todo
# ------------------------------------------------------------------


@router.patch("/{video_id}/status")
async def update_video_status(
    channel_id: str,
    video_id: str,
    body: VideoStatusUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Toggle video status between ``done`` and ``todo``."""
    result = await db.videos.update_one(
        {"channel_id": channel_id, "video_id": video_id},
        {
            "$set": {
                "status": body.status.value,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video {video_id} not found",
        )

    # Update category video count when marking done.
    if body.status == VideoStatus.DONE:
        video = await db.videos.find_one(
            {"channel_id": channel_id, "video_id": video_id}
        )
        if video and video.get("category"):
            await db.categories.update_one(
                {"channel_id": channel_id, "name": video["category"]},
                {"$inc": {"video_count": 1}},
            )

    return {"ok": True, "video_id": video_id, "status": body.status.value}


# ------------------------------------------------------------------
# POST /queue  –  add video to posting queue (+ videos collection)
# ------------------------------------------------------------------


@router.post("/queue", status_code=status.HTTP_201_CREATED)
async def add_to_queue(
    channel_id: str,
    body: VideoCreate,
    file: UploadFile = File(...),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Create a video record **and** add it to the posting queue.

    The video file is streamed to R2 storage.
    """
    r2 = _get_r2()
    video_id = str(uuid.uuid4())
    r2_key = f"{channel_id}/{video_id}.mp4"

    # Stream file to R2.
    r2.upload_video(file.file, r2_key)

    # Insert video document.
    now = datetime.utcnow()
    video_doc = {
        "channel_id": channel_id,
        "video_id": video_id,
        "title": body.title,
        "description": body.description,
        "tags": body.tags,
        "category": body.category,
        "topic": body.topic,
        "status": "in_queue",
        "suggested": False,
        "basis_factor": body.basis_factor,
        "youtube_video_id": None,
        "r2_object_key": r2_key,
        "metadata": {
            "views": None,
            "engagement": None,
            "avg_percentage_viewed": None,
        },
        "created_at": now,
        "updated_at": now,
    }
    await db.videos.insert_one(video_doc)

    # Determine next position in queue.
    last = await db.video_queue.find_one(
        {"channel_id": channel_id},
        sort=[("position", -1)],
    )
    next_pos = (last["position"] + 1) if last else 1

    await db.video_queue.insert_one(
        {
            "channel_id": channel_id,
            "video_id": video_id,
            "position": next_pos,
            "added_at": now,
        }
    )

    video_doc.pop("_id", None)
    return {"ok": True, "video": video_doc, "queue_position": next_pos}
