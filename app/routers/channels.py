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

@router.get("/")
async def list_channels(
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return all registered channels (tokens excluded)."""
    channels = await db.channels.find(
        {}, {"youtube_tokens": 0}
    ).to_list(length=None)
    for c in channels:
        c["_id"] = str(c["_id"])
    return channels


# ------------------------------------------------------------------
# GET /{channel_id}  –  get a single channel
# ------------------------------------------------------------------


@router.get("/{channel_id}")
async def get_channel(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return a single channel by its ``channel_id`` (tokens excluded)."""
    doc = await db.channels.find_one(
        {"channel_id": channel_id}, {"youtube_tokens": 0}
    )
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
        await manager.get_service(channel_id_for_token) if channel_id_for_token else None
    )
    if yt is None:
        # Fall back to any available cached service instance.
        if manager._cache:
            yt = next(iter(manager._cache.values()))
        else:
            # Try to find any channel with stored tokens.
            channels_with_tokens = await db.channels.find(
                {"youtube_tokens": {"$exists": True, "$ne": None}},
                {"channel_id": 1},
            ).to_list(length=1)
            for ch in channels_with_tokens:
                yt = await manager.get_service(ch["channel_id"])
                if yt:
                    break
    if yt is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No YouTube token available. Store tokens via POST /channels/{id}/youtube-token",
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
    yt = await manager.get_service(channel_id)
    if yt is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No YouTube token for channel '{channel_id}'. Store tokens via POST /channels/{channel_id}/youtube-token",
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
    unique: bool = Field(False, description="If True, Gemini must not reuse existing values when generating new videos")


class ContentParamUpdate(BaseModel):
    """Payload for updating an existing content param."""
    description: Optional[str] = None
    values: Optional[list[str]] = None
    belongs_to: Optional[list[str]] = None
    unique: Optional[bool] = None


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
        "unique": body.unique,
        "created_at": now,
        "updated_at": now,
    }
    await db.content_params.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.patch("/{channel_id}/content-params/{param_name}")
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

    if body.unique is not None:
        update_fields["unique"] = body.unique

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
# Competitors CRUD  –  stored in the ``competitors`` collection
# ------------------------------------------------------------------


class CompetitorCreate(BaseModel):
    """Payload for adding a competitor channel."""
    youtube_channel_id: str = Field(..., description="Competitor's YouTube channel ID")
    handle: str = Field(..., description="YouTube handle, e.g. @MrBeast")
    name: str = Field(..., description="Display name")
    thumbnail: str = Field("", description="Thumbnail/avatar URL")


@router.get("/{channel_id}/competitors")
async def list_competitors(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List all competitors for a channel."""
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Channel '{channel_id}' not found")

    docs = await db.competitors.find({"channel_id": channel_id}).to_list(length=None)
    for d in docs:
        d.pop("_id", None)
    return {"channel_id": channel_id, "competitors": docs}


@router.post("/{channel_id}/competitors", status_code=status.HTTP_201_CREATED)
async def add_competitor(
    channel_id: str,
    body: CompetitorCreate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Add a competitor to a channel."""
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Channel '{channel_id}' not found")

    existing = await db.competitors.find_one({
        "channel_id": channel_id,
        "youtube_channel_id": body.youtube_channel_id,
    })
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Competitor '{body.youtube_channel_id}' already exists for this channel",
        )

    doc = {
        "channel_id": channel_id,
        "youtube_channel_id": body.youtube_channel_id,
        "handle": body.handle,
        "name": body.name,
        "thumbnail": body.thumbnail,
        "created_at": now_ist(),
    }
    await db.competitors.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.delete("/{channel_id}/competitors/{competitor_yt_id}")
async def remove_competitor(
    channel_id: str,
    competitor_yt_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Remove a competitor from a channel."""
    result = await db.competitors.delete_one({
        "channel_id": channel_id,
        "youtube_channel_id": competitor_yt_id,
    })
    if result.deleted_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Competitor '{competitor_yt_id}' not found for channel '{channel_id}'",
        )
    return {"ok": True, "deleted": competitor_yt_id}


# ------------------------------------------------------------------
# YouTube OAuth config  –  stored in the ``config`` collection
# ------------------------------------------------------------------


class YouTubeOAuthConfig(BaseModel):
    """Client credentials for the Google OAuth app."""
    client_id: str = Field(..., description="Google OAuth client ID")
    client_secret: str = Field(..., description="Google OAuth client secret")


@router.put("/config/youtube-oauth", tags=["config"])
async def set_youtube_oauth_config(
    body: YouTubeOAuthConfig,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Store or update the YouTube OAuth client credentials in the DB."""
    await db.config.update_one(
        {"key": "youtube_oauth"},
        {
            "$set": {
                "key": "youtube_oauth",
                "client_id": body.client_id,
                "client_secret": body.client_secret,
                "updated_at": now_ist(),
            }
        },
        upsert=True,
    )
    return {"ok": True, "message": "YouTube OAuth config saved"}


@router.get("/config/youtube-oauth", tags=["config"])
async def get_youtube_oauth_config_endpoint(
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Check if YouTube OAuth client credentials are configured."""
    from app.database import get_youtube_oauth_config

    doc = await get_youtube_oauth_config(db)
    if not doc:
        return {"configured": False}
    return {
        "configured": True,
        "client_id": doc["client_id"],
    }


# ------------------------------------------------------------------
# YouTube token management  –  stored on the channel document
# ------------------------------------------------------------------


class YouTubeTokenStore(BaseModel):
    """Payload for storing OAuth tokens from the frontend."""
    token: str = Field(..., description="OAuth2 access token")
    refresh_token: str = Field(..., description="OAuth2 refresh token")
    token_uri: str = Field("https://oauth2.googleapis.com/token")
    scopes: list[str] = Field(default_factory=list)
    expiry: Optional[str] = Field(None, description="ISO 8601 expiry datetime of the access token")


@router.post("/{channel_id}/youtube-token")
async def store_youtube_token(
    channel_id: str,
    body: YouTubeTokenStore,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Store YouTube OAuth tokens on a channel document.

    Called by the frontend after the user completes the Google OAuth
    consent flow and receives tokens client-side.
    """
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    token_doc = {
        "token": body.token,
        "refresh_token": body.refresh_token,
        "token_uri": body.token_uri,
        "scopes": body.scopes,
        "expiry": body.expiry,
    }

    await db.channels.update_one(
        {"channel_id": channel_id},
        {"$set": {"youtube_tokens": token_doc, "updated_at": now_ist()}},
    )

    manager = _get_youtube_manager()
    manager.invalidate(channel_id)

    return {"ok": True, "channel_id": channel_id, "message": "YouTube tokens stored"}


@router.get("/{channel_id}/youtube-token")
async def get_youtube_token(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return a fresh access token for the channel.

    If the stored access token is expired, it is automatically refreshed
    using the refresh token and the updated token is saved back to the DB.
    Only the short-lived access token is returned — never the refresh token.
    """
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    tokens = channel.get("youtube_tokens")
    if not tokens:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No YouTube tokens stored for channel '{channel_id}'",
        )

    from app.database import get_youtube_oauth_config
    from app.config import get_settings
    from google.oauth2.credentials import Credentials
    from datetime import timezone

    settings = get_settings()
    oauth_cfg = await get_youtube_oauth_config(db)
    client_id = (oauth_cfg or {}).get("client_id") or settings.YOUTUBE_CLIENT_ID
    client_secret = (oauth_cfg or {}).get("client_secret") or settings.YOUTUBE_CLIENT_SECRET

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="YouTube OAuth client credentials not configured",
        )

    expiry = tokens.get("expiry")
    expiry_dt = None
    if expiry:
        from datetime import datetime as dt
        try:
            expiry_dt = dt.fromisoformat(expiry.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            expiry_dt = None

    creds = Credentials(
        token=tokens["token"],
        refresh_token=tokens["refresh_token"],
        token_uri=tokens.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=client_id,
        client_secret=client_secret,
        scopes=tokens.get("scopes"),
        expiry=expiry_dt.replace(tzinfo=None) if expiry_dt else None,
    )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())

            updated_expiry = creds.expiry.replace(tzinfo=timezone.utc).isoformat() if creds.expiry else None
            await db.channels.update_one(
                {"channel_id": channel_id},
                {
                    "$set": {
                        "youtube_tokens.token": creds.token,
                        "youtube_tokens.expiry": updated_expiry,
                        "updated_at": now_ist(),
                    }
                },
            )
            manager = _get_youtube_manager()
            manager.invalidate(channel_id)
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="YouTube token is invalid and cannot be refreshed. Re-authenticate via the frontend.",
            )

    return {"ok": True, "access_token": creds.token, "expiry": creds.expiry.isoformat() + "Z" if creds.expiry else None}


@router.get("/{channel_id}/youtube-token/status")
async def get_youtube_token_status(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Check YouTube token status without exposing token values."""
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    tokens = channel.get("youtube_tokens")
    if not tokens:
        return {"channel_id": channel_id, "connected": False, "status": "disconnected"}

    expiry = tokens.get("expiry")
    if expiry:
        from datetime import datetime as dt, timezone
        try:
            expiry_dt = dt.fromisoformat(expiry.replace("Z", "+00:00"))
            is_expired = expiry_dt <= dt.now(timezone.utc)
        except (ValueError, TypeError):
            is_expired = True
    else:
        is_expired = True

    has_refresh = bool(tokens.get("refresh_token"))

    if is_expired and has_refresh:
        token_status = "expired_refreshable"
    elif is_expired:
        token_status = "expired"
    else:
        token_status = "active"

    return {
        "channel_id": channel_id,
        "connected": True,
        "status": token_status,
        "has_refresh_token": has_refresh,
        "expiry": expiry,
    }


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
    await db.competitors.delete_many({"channel_id": channel_id})

    return {"ok": True, "channel_id": channel_id, "deleted": True}
