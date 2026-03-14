"""Channels router – register and manage YouTube channels.

On registration, channel metadata is automatically fetched from YouTube.
"""

from datetime import datetime
from typing import Optional

from app.timezone import now_ist

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

    now = now_ist()
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

    update_data["updated_at"] = now_ist()

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
        "updated_at": now_ist(),
    }

    await db.channels.update_one(
        {"channel_id": channel_id},
        {"$set": update},
    )
    return {"ok": True, "channel_id": channel_id, "updated": update}


# ------------------------------------------------------------------
# Content params CRUD  –  stored in the ``content_params`` collection
# ------------------------------------------------------------------


class ContentParamCreate(BaseModel):
    """Payload for adding a new content param."""
    name: str = Field(..., description="Parameter key, e.g. 'simulation_type'")
    description: str = Field("", description="What this parameter represents")
    values: list[str] = Field(default_factory=list, description="Allowed values. Empty = free-form.")
    belongs_to: list[str] = Field(default_factory=lambda: ["all"], description="Categories this applies to. ['all'] = every category.")


class ContentParamUpdate(BaseModel):
    """Payload for updating an existing content param."""
    description: Optional[str] = None
    values: Optional[list[str]] = None
    belongs_to: Optional[list[str]] = None


@router.get("/{channel_id}/content-params")
async def list_content_params(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List all content param definitions for a channel."""
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Channel '{channel_id}' not found")

    docs = await db.content_params.find({"channel_id": channel_id}).to_list(length=None)
    for d in docs:
        d.pop("_id", None)
    return docs


@router.post("/{channel_id}/content-params", status_code=status.HTTP_201_CREATED)
async def create_content_param(
    channel_id: str,
    body: ContentParamCreate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Add a new content param definition for a channel."""
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Channel '{channel_id}' not found")

    existing = await db.content_params.find_one({"channel_id": channel_id, "name": body.name})
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Content param '{body.name}' already exists for this channel")

    now = now_ist()
    doc = {
        "channel_id": channel_id,
        "name": body.name,
        "description": body.description,
        "values": [{"value": v, "score": 0, "video_count": 0} for v in body.values],
        "belongs_to": body.belongs_to,
        "created_at": now,
        "updated_at": now,
    }
    await db.content_params.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.put("/{channel_id}/content-params/{param_name}")
async def update_content_param(
    channel_id: str,
    param_name: str,
    body: ContentParamUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Update an existing content param (description, values, belongs_to)."""
    existing = await db.content_params.find_one({"channel_id": channel_id, "name": param_name})
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Content param '{param_name}' not found for channel '{channel_id}'")

    update_fields: dict = {"updated_at": now_ist()}

    if body.description is not None:
        update_fields["description"] = body.description

    if body.belongs_to is not None:
        update_fields["belongs_to"] = body.belongs_to

    if body.values is not None:
        existing_values_map = {v["value"]: v for v in existing.get("values", [])}
        new_values = []
        for v in body.values:
            if v in existing_values_map:
                new_values.append(existing_values_map[v])
            else:
                new_values.append({"value": v, "score": 0, "video_count": 0})
        update_fields["values"] = new_values

    await db.content_params.update_one({"_id": existing["_id"]}, {"$set": update_fields})

    updated = await db.content_params.find_one({"_id": existing["_id"]})
    updated.pop("_id", None)
    return updated


@router.delete("/{channel_id}/content-params/{param_name}")
async def delete_content_param(
    channel_id: str,
    param_name: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Remove a content param definition."""
    result = await db.content_params.delete_one({"channel_id": channel_id, "name": param_name})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Content param '{param_name}' not found for channel '{channel_id}'")
    return {"ok": True, "channel_id": channel_id, "deleted_param": param_name}


# ------------------------------------------------------------------
# DELETE /{channel_id}  –  remove a channel
# ------------------------------------------------------------------


@router.delete("/{channel_id}")
async def delete_channel(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Remove a channel and all its associated data, including R2 files."""
    doc = await db.channels.find_one({"channel_id": channel_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    # 1. Clean up R2 storage
    # Fetch all videos with an R2 key to delete files first
    videos_with_files = await db.videos.find(
        {"channel_id": channel_id, "r2_object_key": {"$ne": None}},
        {"r2_object_key": 1}
    ).to_list(length=None)

    if videos_with_files:
        from app.routers.videos import _get_r2
        try:
            r2 = _get_r2()
            for v in videos_with_files:
                r2.delete_video(v["r2_object_key"])
        except Exception as exc:
            # We log but continue, as orphaned R2 files are better than failing deletion
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Cleanup of R2 files for channel {channel_id} partially failed: {exc}")

    # 2. Delete channel and all scoped data from DB.
    await db.channels.delete_one({"channel_id": channel_id})
    await db.videos.delete_many({"channel_id": channel_id})
    await db.posting_queue.delete_many({"channel_id": channel_id})
    await db.schedule_queue.delete_many({"channel_id": channel_id})
    await db.categories.delete_many({"channel_id": channel_id})
    await db.analysis.delete_many({"channel_id": channel_id})
    await db.analysis_history.delete_many({"channel_id": channel_id})
    await db.content_params.delete_many({"channel_id": channel_id})

    return {"ok": True, "channel_id": channel_id, "deleted": True}
