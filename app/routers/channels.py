"""Channels router – register and manage YouTube channels.

On registration, channel metadata is automatically fetched from YouTube.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key


router = APIRouter(
    prefix="/api/v1/channels",
    tags=["channels"],
    dependencies=[Depends(verify_api_key)],
)


def _get_youtube_manager():
    """Lazy import to avoid circular dependency."""
    from app.main import youtube_service_manager  # type: ignore[import]

    if youtube_service_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="YouTube service manager not initialised",
        )
    return youtube_service_manager


# ------------------------------------------------------------------
# Request / response models
# ------------------------------------------------------------------


class ChannelCreate(BaseModel):
    """Payload for registering a new channel.

    Only ``youtube_channel_id`` is required. The ``channel_id`` slug is
    optional — if omitted, the YouTube custom URL or channel name is
    used to generate one.
    """

    youtube_channel_id: str = Field(..., description="YouTube UC... channel ID")
    channel_id: Optional[str] = Field(
        None, description="Custom internal slug. Auto-generated if omitted."
    )


class ChannelUpdate(BaseModel):
    """Partial update payload."""

    name: Optional[str] = None


# ------------------------------------------------------------------
# GET /  –  list all channels
# ------------------------------------------------------------------


from app.models.channel import Channel

@router.get("/", response_model=list[Channel])
async def list_channels(
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return all registered channels."""
    channels = await db.channels.find().to_list(length=None)
    for c in channels:
        c["_id"] = str(c["_id"])
    return channels


# ------------------------------------------------------------------
# GET /{channel_id}  –  get a single channel
# ------------------------------------------------------------------


@router.get("/{channel_id}", response_model=Channel)
async def get_channel(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return a single channel by its ``channel_id``."""
    doc = await db.channels.find_one({"channel_id": channel_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )
    doc["_id"] = str(doc["_id"])
    return doc


# ------------------------------------------------------------------
# POST /  –  register a new channel (auto-fetches from YouTube)
# ------------------------------------------------------------------


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=Channel)
async def create_channel(
    body: ChannelCreate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Register a new channel by fetching its data from YouTube.

    Provide the ``youtube_channel_id`` (the UC... ID) and the server
    will fetch the channel name, description, subscriber count, etc.
    """
    manager = _get_youtube_manager()

    # For channel registration we need any valid YouTube client (read-only).
    # Use the new channel_id's token if it exists, otherwise any cached one.
    channel_id_for_token = body.channel_id
    yt = (
        manager.get_service(channel_id_for_token) if channel_id_for_token else None
    )
    if yt is None:
        # Fall back to any available service instance.
        if manager._cache:
            yt = next(iter(manager._cache.values()))
        else:
            # Try to load any token file from the tokens dir.
            import os
            for f in os.listdir(manager._tokens_dir):
                if f.endswith(".json"):
                    cid = f.removesuffix(".json")
                    yt = manager.get_service(cid)
                    if yt:
                        break
    if yt is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No YouTube token available. Generate one with: python generate_youtube_token.py <channel_id>",
        )

    # Fetch channel data from YouTube.
    try:
        yt_data = yt.get_channel_info(body.youtube_channel_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )

    # Generate channel_id slug if not provided.
    channel_id = body.channel_id
    if not channel_id:
        # Use custom URL or cleaned channel name as slug.
        raw = yt_data.get("custom_url", "") or yt_data.get("name", "")
        channel_id = raw.lower().lstrip("@").replace(" ", "-")

    if not channel_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not generate channel_id — please provide one explicitly",
        )

    # Check for duplicate.
    existing = await db.channels.find_one({"channel_id": channel_id})
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Channel '{channel_id}' already exists",
        )

    now = datetime.utcnow()
    doc = {
        "channel_id": channel_id,
        "youtube_channel_id": body.youtube_channel_id,
        "name": yt_data["name"],
        "description": yt_data.get("description", ""),
        "custom_url": yt_data.get("custom_url", ""),
        "thumbnail_url": yt_data.get("thumbnail_url", ""),
        "subscriber_count": yt_data.get("subscriber_count", 0),
        "video_count": yt_data.get("video_count", 0),
        "view_count": yt_data.get("view_count", 0),
        "created_at": now,
        "updated_at": now,
    }
    await db.channels.insert_one(doc)
    doc["_id"] = str(doc["_id"])
    return doc


# ------------------------------------------------------------------
# PATCH /{channel_id}  –  update a channel
# ------------------------------------------------------------------


@router.patch("/{channel_id}")
async def update_channel(
    channel_id: str,
    body: ChannelUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Partially update a channel."""
    update_data = body.model_dump(exclude_none=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    update_data["updated_at"] = datetime.utcnow()

    result = await db.channels.update_one(
        {"channel_id": channel_id},
        {"$set": update_data},
    )
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )
    return {"ok": True, "channel_id": channel_id}


# ------------------------------------------------------------------
# POST /{channel_id}/refresh  –  re-fetch data from YouTube
# ------------------------------------------------------------------


@router.post("/{channel_id}/refresh")
async def refresh_channel(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Re-fetch channel data from YouTube and update the DB."""
    doc = await db.channels.find_one({"channel_id": channel_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    manager = _get_youtube_manager()
    yt = manager.get_service(channel_id)
    if yt is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No YouTube token for channel '{channel_id}'. Run: python generate_youtube_token.py {channel_id}",
        )
    try:
        yt_data = yt.get_channel_info(doc["youtube_channel_id"])
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )

    update = {
        "name": yt_data["name"],
        "description": yt_data.get("description", ""),
        "custom_url": yt_data.get("custom_url", ""),
        "thumbnail_url": yt_data.get("thumbnail_url", ""),
        "subscriber_count": yt_data.get("subscriber_count", 0),
        "video_count": yt_data.get("video_count", 0),
        "view_count": yt_data.get("view_count", 0),
        "updated_at": datetime.utcnow(),
    }

    await db.channels.update_one(
        {"channel_id": channel_id},
        {"$set": update},
    )
    return {"ok": True, "channel_id": channel_id, "updated": update}


# ------------------------------------------------------------------
# DELETE /{channel_id}  –  remove a channel
# ------------------------------------------------------------------


@router.delete("/{channel_id}")
async def delete_channel(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Remove a channel and all its associated data."""
    doc = await db.channels.find_one({"channel_id": channel_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    # Delete channel and all scoped data.
    await db.channels.delete_one({"channel_id": channel_id})
    await db.videos.delete_many({"channel_id": channel_id})
    await db.posting_queue.delete_many({"channel_id": channel_id})
    await db.schedule_queue.delete_many({"channel_id": channel_id})
    await db.categories.delete_many({"channel_id": channel_id})
    await db.analysis.delete_many({"channel_id": channel_id})
    await db.analysis_history.delete_many({"channel_id": channel_id})

    return {"ok": True, "channel_id": channel_id, "deleted": True}
