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
from app.logger import get_logger

logger = get_logger(__name__)


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


def _get_instagram_manager():
    """Lazy import to avoid circular dependency."""
    from app.main import instagram_service_manager  # type: ignore[import]

    if instagram_service_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Instagram service manager not initialised",
        )
    return instagram_service_manager


# ------------------------------------------------------------------
# Request / response models
# ------------------------------------------------------------------


class ChannelCreate(BaseModel):
    """Payload for registering a new channel.

    For YouTube: ``youtube_channel_id`` is required.
    For Instagram: ``instagram_user_id`` and ``access_token`` are required.
    """

    platform: str = Field("youtube", description="'youtube' or 'instagram'")
    youtube_channel_id: Optional[str] = Field(None, description="YouTube UC... channel ID (required for youtube)")
    instagram_user_id: Optional[str] = Field(None, description="Instagram user ID (required for instagram)")
    access_token: Optional[str] = Field(None, description="Long-lived Instagram access token (required for instagram)")
    expires_at: Optional[str] = Field(None, description="ISO 8601 token expiry datetime (optional, for instagram)")
    channel_id: Optional[str] = Field(
        None, description="Custom internal slug. Auto-generated if omitted."
    )


class ChannelUpdate(BaseModel):
    """Partial update payload."""

    name: Optional[str] = None
    default_description: Optional[str] = None
    default_tags: Optional[list[str]] = None


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
        {}, {"youtube_tokens": 0, "instagram_tokens": 0}
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
        {"channel_id": channel_id}, {"youtube_tokens": 0, "instagram_tokens": 0}
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


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_channel(
    body: ChannelCreate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Register a new channel by fetching its data from YouTube or Instagram.

    For YouTube: provide ``youtube_channel_id``.
    For Instagram: provide ``instagram_user_id``.
    """
    platform = body.platform.lower()
    if platform not in ("youtube", "instagram"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported platform '{platform}'. Use 'youtube' or 'instagram'.",
        )

    if platform == "instagram":
        return await _create_instagram_channel(body, db)

    # --- YouTube flow (existing) ---
    if not body.youtube_channel_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="youtube_channel_id is required for YouTube channels",
        )

    manager = _get_youtube_manager()
    channel_id_for_token = body.channel_id
    yt = (
        await manager.get_service(channel_id_for_token) if channel_id_for_token else None
    )
    if yt is None:
        if manager._cache:
            yt = next(iter(manager._cache.values()))
        else:
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

    try:
        yt_data = yt.get_channel_info(body.youtube_channel_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    channel_id = body.channel_id
    if not channel_id:
        raw = yt_data.get("custom_url", "") or yt_data.get("name", "")
        channel_id = raw.lower().lstrip("@").replace(" ", "-")

    if not channel_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not generate channel_id — please provide one explicitly",
        )

    existing = await db.channels.find_one({"channel_id": channel_id})
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Channel '{channel_id}' already exists",
        )

    now = now_ist()
    doc = {
        "channel_id": channel_id,
        "platform": "youtube",
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


async def _create_instagram_channel(
    body: ChannelCreate,
    db: AsyncIOMotorDatabase,
) -> dict:
    """Register a new Instagram channel.

    Accepts ``access_token`` directly in the request body so there is no
    chicken-and-egg dependency on an existing channel.  The token is stored
    on the new channel document and used immediately to fetch account info.
    """
    if not body.instagram_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="instagram_user_id is required for Instagram channels",
        )
    if not body.access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="access_token is required for Instagram channels",
        )

    from app.services.instagram import InstagramService

    ig_svc = InstagramService(access_token=body.access_token)

    try:
        ig_data = ig_svc.get_account_info(body.instagram_user_id)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    channel_id = body.channel_id
    if not channel_id:
        raw = ig_data.get("username", "")
        channel_id = raw.lower().replace(" ", "-")
        if channel_id:
            channel_id = f"{channel_id}-ig"

    if not channel_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not generate channel_id — please provide one explicitly",
        )

    existing = await db.channels.find_one({"channel_id": channel_id})
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Channel '{channel_id}' already exists",
        )

    token_doc = {"access_token": body.access_token}
    if body.expires_at:
        token_doc["expires_at"] = body.expires_at

    now = now_ist()
    doc = {
        "channel_id": channel_id,
        "platform": "instagram",
        "instagram_user_id": body.instagram_user_id,
        "instagram_tokens": token_doc,
        "name": ig_data.get("name") or ig_data.get("username", channel_id),
        "description": ig_data.get("biography", ""),
        "thumbnail_url": ig_data.get("profile_picture_url", ""),
        "subscriber_count": ig_data.get("followers_count", 0),
        "video_count": ig_data.get("media_count", 0),
        "view_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    await db.channels.insert_one(doc)
    doc["_id"] = str(doc["_id"])
    doc.pop("instagram_tokens", None)

    logger.success("Registered Instagram channel '%s' (user_id=%s)", channel_id, body.instagram_user_id)
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
    """Re-fetch channel data from the appropriate platform and update the DB."""
    doc = await db.channels.find_one({"channel_id": channel_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    platform = doc.get("platform", "youtube")

    if platform == "instagram":
        return await _refresh_instagram_channel(channel_id, doc, db)

    # --- YouTube ---
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

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
    await db.channels.update_one({"channel_id": channel_id}, {"$set": update})
    return {"ok": True, "channel_id": channel_id, "updated": update}


async def _refresh_instagram_channel(
    channel_id: str,
    doc: dict,
    db: AsyncIOMotorDatabase,
) -> dict:
    """Re-fetch Instagram account data from Graph API."""
    mgr = _get_instagram_manager()
    ig_svc = await mgr.get_service(channel_id)
    if ig_svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No Instagram token for channel '{channel_id}'. Store tokens via POST /channels/{channel_id}/instagram-token",
        )

    ig_user_id = doc.get("instagram_user_id")
    if not ig_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Channel '{channel_id}' has no instagram_user_id",
        )

    try:
        ig_data = ig_svc.get_account_info(ig_user_id)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))

    update = {
        "name": ig_data.get("name") or ig_data.get("username", doc.get("name", "")),
        "description": ig_data.get("biography", ""),
        "thumbnail_url": ig_data.get("profile_picture_url", ""),
        "subscriber_count": ig_data.get("followers_count", 0),
        "video_count": ig_data.get("media_count", 0),
        "updated_at": now_ist(),
    }
    await db.channels.update_one({"channel_id": channel_id}, {"$set": update})
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


@router.post("/{channel_id}/content-params/sync", status_code=status.HTTP_200_OK)
async def sync_content_params_on_videos(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Strip orphaned keys from ``videos.content_params`` after param definitions are removed.

    For each video on the channel, keeps only keys that still exist in the
    ``content_params`` collection. Empty objects become ``null``.
    """
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Channel '{channel_id}' not found")

    valid_names = {
        d["name"]
        async for d in db.content_params.find({"channel_id": channel_id}, {"name": 1})
    }

    videos = await db.videos.find(
        {"channel_id": channel_id, "content_params": {"$ne": None}},
    ).to_list(length=None)

    updated_count = 0
    keys_removed_total = 0

    for v in videos:
        cp = v.get("content_params")
        if not isinstance(cp, dict):
            continue
        filtered = {k: val for k, val in cp.items() if k in valid_names}
        removed = len(cp) - len(filtered)
        if removed == 0 and filtered == cp:
            continue
        keys_removed_total += removed
        new_cp = filtered if filtered else None
        await db.videos.update_one(
            {"_id": v["_id"]},
            {"$set": {"content_params": new_cp, "updated_at": now_ist()}},
        )
        updated_count += 1

    logger.success(
        "Synced content params on videos for channel '%s': %d video(s) updated, %d key(s) removed",
        channel_id,
        updated_count,
        keys_removed_total,
    )

    return {
        "ok": True,
        "channel_id": channel_id,
        "valid_param_names": sorted(valid_names),
        "videos_scanned": len(videos),
        "videos_updated": updated_count,
        "orphan_keys_removed": keys_removed_total,
    }


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
    youtube_channel_id: Optional[str] = Field(None, description="Competitor's YouTube channel ID")
    handle: Optional[str] = Field(None, description="YouTube handle, e.g. @MrBeast")
    instagram_username: Optional[str] = Field(None, description="Instagram username, e.g. 'mrbeast'")
    name: Optional[str] = Field(None, description="Display name (auto-fetched if omitted)")
    thumbnail: Optional[str] = Field(None, description="Thumbnail/avatar URL (auto-fetched if omitted)")


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
    """Add a competitor to a channel (YouTube or Instagram)."""
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Channel '{channel_id}' not found")

    platform = channel.get("platform", "youtube")
    comp_platform = "instagram" if body.instagram_username else "youtube"

    # -- Validate input --
    if comp_platform == "youtube" and not body.youtube_channel_id:
        raise HTTPException(status_code=400, detail="youtube_channel_id is required for YouTube competitors")
    if comp_platform == "instagram" and not body.instagram_username:
        raise HTTPException(status_code=400, detail="instagram_username is required for Instagram competitors")

    # -- Check for existing --
    search_query = {"channel_id": channel_id}
    if comp_platform == "youtube":
        search_query["youtube_channel_id"] = body.youtube_channel_id
    else:
        search_query["instagram_username"] = body.instagram_username

    existing = await db.competitors.find_one(search_query)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Competitor '{body.youtube_channel_id or body.instagram_username}' already exists for this channel",
        )

    # -- Fetch metadata if missing --
    name = body.name
    thumbnail = body.thumbnail
    sub_count = 0
    vid_count = 0
    comp_yt_id = body.youtube_channel_id
    comp_ig_user_id = None

    if comp_platform == "youtube":
        manager = _get_youtube_manager()
        yt = await manager.get_service(channel_id)
        if yt:
            try:
                info = yt.get_channel_info(body.youtube_channel_id)
                name = name or info.get("name")
                thumbnail = thumbnail or info.get("thumbnail_url")
                sub_count = info.get("subscriber_count", 0)
                vid_count = info.get("video_count", 0)
            except Exception as e:
                logger.warning("Could not fetch YouTube competitor metadata: %s", e)
    else:
        # Instagram competitor fetching via Business Discovery
        manager = _get_instagram_manager()
        ig = await manager.get_service(channel_id)
        own_ig_id = channel.get("instagram_user_id")
        if ig and own_ig_id:
            try:
                info = ig.discover_business_account(own_ig_id, body.instagram_username)
                name = name or info.get("name")
                thumbnail = thumbnail or info.get("profile_picture_url")
                sub_count = info.get("followers_count", 0)
                vid_count = info.get("media_count", 0)
                comp_ig_user_id = info.get("instagram_user_id")
            except Exception as e:
                logger.warning("Could not fetch Instagram competitor metadata: %s", e)

    doc = {
        "channel_id": channel_id,
        "platform": comp_platform,
        "youtube_channel_id": comp_yt_id,
        "instagram_username": body.instagram_username,
        "instagram_user_id": comp_ig_user_id,
        "handle": body.handle,
        "name": name or body.instagram_username or body.handle or "Unknown",
        "thumbnail": thumbnail or "",
        "subscriber_count": sub_count,
        "video_count": vid_count,
        "created_at": now_ist(),
        "updated_at": now_ist(),
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
    expiry: Optional[str] = Field(None, description="ISO 8601 expiry datetime of the REFRESH token (set by frontend)")


@router.post("/{channel_id}/youtube-token")
async def store_youtube_token(
    channel_id: str,
    body: YouTubeTokenStore,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Store YouTube OAuth tokens on a channel document.

    Called by the frontend after the user completes the Google OAuth
    consent flow and receives tokens client-side.
    The `expiry` field from the frontend is treated as the refresh token expiry.
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
        "refresh_token_expiry": body.expiry,
        "access_token_expiry": None,
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

    from datetime import datetime as dt

    # Parse access_token_expiry from DB (managed by server)
    access_expiry_raw = tokens.get("access_token_expiry")
    access_expiry_dt = None
    if access_expiry_raw:
        try:
            access_expiry_dt = dt.fromisoformat(access_expiry_raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            access_expiry_dt = None

    creds = Credentials(
        token=tokens["token"],
        refresh_token=tokens["refresh_token"],
        token_uri=tokens.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=client_id,
        client_secret=client_secret,
        scopes=tokens.get("scopes"),
        expiry=access_expiry_dt.astimezone(timezone.utc).replace(tzinfo=None) if access_expiry_dt else None,
    )

    refreshed = False

    # If no access_token_expiry stored, we don't know if it's valid → must check with Google
    needs_refresh = False
    if access_expiry_dt is None:
        # No stored access token expiry → call Google to validate/refresh
        logger.info(f"[TOKEN] No access_token_expiry stored for channel '{channel_id}'. Will refresh via Google.")
        needs_refresh = True
    elif not creds.valid:
        # Stored expiry says it's expired
        logger.info(f"[TOKEN] Access token for channel '{channel_id}' is EXPIRED (expiry: {access_expiry_raw}). Will refresh.")
        needs_refresh = True

    if needs_refresh and creds.refresh_token:
        from google.auth.transport.requests import Request
        try:
            creds.refresh(Request())
            refreshed = True

            # Save the new access token and its expiry back to DB
            updated_access_expiry = None
            if creds.expiry:
                updated_access_expiry = creds.expiry.replace(tzinfo=timezone.utc).isoformat()

            await db.channels.update_one(
                {"channel_id": channel_id},
                {
                    "$set": {
                        "youtube_tokens.token": creds.token,
                        "youtube_tokens.access_token_expiry": updated_access_expiry,
                        "updated_at": now_ist(),
                    }
                },
            )
            manager = _get_youtube_manager()
            manager.invalidate(channel_id)
        except Exception as e:
            logger.error(f"[TOKEN] Failed to refresh access token for channel '{channel_id}': {e}")

    # Final check: if still invalid, raise error
    if not creds.valid:
        logger.error(f"[TOKEN] YouTube token for channel '{channel_id}' is INVALID and could not be refreshed.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="YouTube token is invalid and cannot be refreshed. Re-authenticate via the frontend.",
        )

    if refreshed:
        logger.info(f"[TOKEN] YouTube access token for channel '{channel_id}' was REFRESHED using refresh token.")
    else:
        logger.info(f"[TOKEN] YouTube access token for channel '{channel_id}' was already VALID (expiry: {access_expiry_raw}).")

    return {
        "ok": True,
        "access_token": creds.token,
        "access_token_expiry": creds.expiry.isoformat() + "Z" if creds.expiry else None,
        "refresh_token_expiry": tokens.get("refresh_token_expiry"),
    }

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

    from app.database import get_youtube_oauth_config
    from app.config import get_settings
    from google.oauth2.credentials import Credentials
    from datetime import datetime as dt, timezone

    settings = get_settings()
    oauth_cfg = await get_youtube_oauth_config(db)
    client_id = (oauth_cfg or {}).get("client_id") or settings.YOUTUBE_CLIENT_ID
    client_secret = (oauth_cfg or {}).get("client_secret") or settings.YOUTUBE_CLIENT_SECRET

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="YouTube OAuth client credentials not configured",
        )

    # Parse access_token_expiry from DB
    access_expiry_raw = tokens.get("access_token_expiry")
    access_expiry_dt = None
    if access_expiry_raw:
        try:
            access_expiry_dt = dt.fromisoformat(access_expiry_raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            access_expiry_dt = None

    creds = Credentials(
        token=tokens["token"],
        refresh_token=tokens["refresh_token"],
        token_uri=tokens.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=client_id,
        client_secret=client_secret,
        scopes=tokens.get("scopes"),
        expiry=access_expiry_dt.astimezone(timezone.utc).replace(tzinfo=None) if access_expiry_dt else None,
    )

    refreshed = False
    
    # Proactive check/refresh
    needs_refresh = False
    if access_expiry_dt is None:
        logger.info(f"[STATUS] No access_token_expiry for '{channel_id}'. Initializing via refresh.")
        needs_refresh = True
    elif not creds.valid:
        logger.info(f"[STATUS] Access token for '{channel_id}' is EXPIRED. Refreshing.")
        needs_refresh = True

    if needs_refresh and creds.refresh_token:
        from google.auth.transport.requests import Request
        try:
            creds.refresh(Request())
            refreshed = True

            # Save updated data
            updated_access_expiry = None
            if creds.expiry:
                updated_access_expiry = creds.expiry.replace(tzinfo=timezone.utc).isoformat()

            await db.channels.update_one(
                {"channel_id": channel_id},
                {
                    "$set": {
                        "youtube_tokens.token": creds.token,
                        "youtube_tokens.access_token_expiry": updated_access_expiry,
                        "updated_at": now_ist(),
                    }
                },
            )
            # Re-fetch tokens for final status report
            channel = await db.channels.find_one({"channel_id": channel_id})
            tokens = channel.get("youtube_tokens")
            
            manager = _get_youtube_manager()
            manager.invalidate(channel_id)
            logger.info(f"[STATUS] Successfully refreshed token for '{channel_id}'.")
        except Exception as e:
            logger.error(f"[STATUS] Failed to refresh token for '{channel_id}': {e}")

    # Final check for status reporting
    access_expiry = tokens.get("access_token_expiry")
    access_expired = not creds.valid  # Uses current state after potential refresh

    # Check refresh token expiry
    refresh_expiry = tokens.get("refresh_token_expiry")
    refresh_expired = True
    if refresh_expiry:
        try:
            refresh_dt = dt.fromisoformat(refresh_expiry.replace("Z", "+00:00"))
            refresh_expired = refresh_dt <= dt.now(timezone.utc)
        except (ValueError, TypeError):
            refresh_expired = True

    has_refresh = bool(tokens.get("refresh_token"))

    if not creds.valid and has_refresh and not refresh_expired:
        token_status = "access_expired_refreshable"
    elif not creds.valid and refresh_expired:
        token_status = "fully_expired"
    elif not creds.valid:
        token_status = "access_expired"
    else:
        token_status = "active"

    return {
        "channel_id": channel_id,
        "connected": True,
        "status": token_status,
        "has_refresh_token": has_refresh,
        "access_token_expiry": access_expiry,
        "refresh_token_expiry": refresh_expiry,
        "access_token_expired": not creds.valid,
        "refresh_token_expired": refresh_expired,
        "refreshed_during_check": refreshed
    }



# ------------------------------------------------------------------
# Instagram OAuth config  –  stored in the ``config`` collection
# ------------------------------------------------------------------


class InstagramOAuthConfig(BaseModel):
    """Facebook App credentials for Instagram Graph API."""
    app_id: str = Field(..., description="Facebook App ID")
    app_secret: str = Field(..., description="Facebook App Secret")


@router.put("/config/instagram-oauth", tags=["config"])
async def set_instagram_oauth_config(
    body: InstagramOAuthConfig,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Store or update Instagram/Facebook App credentials in the DB."""
    await db.config.update_one(
        {"key": "instagram_oauth"},
        {
            "$set": {
                "key": "instagram_oauth",
                "app_id": body.app_id,
                "app_secret": body.app_secret,
                "updated_at": now_ist(),
            }
        },
        upsert=True,
    )
    return {"ok": True, "message": "Instagram OAuth config saved"}


@router.get("/config/instagram-oauth", tags=["config"])
async def get_instagram_oauth_config_endpoint(
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Check if Instagram/Facebook App credentials are configured."""
    from app.database import get_instagram_oauth_config

    doc = await get_instagram_oauth_config(db)
    if not doc:
        return {"configured": False}
    return {"configured": True, "app_id": doc["app_id"]}


# ------------------------------------------------------------------
# Instagram token management  –  stored on the channel document
# ------------------------------------------------------------------


class InstagramTokenStore(BaseModel):
    """Payload for storing an Instagram long-lived token from the frontend."""
    access_token: str = Field(..., description="Long-lived Facebook user access token")
    expires_at: Optional[str] = Field(None, description="ISO 8601 expiry datetime")


@router.post("/{channel_id}/instagram-token")
async def store_instagram_token(
    channel_id: str,
    body: InstagramTokenStore,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Store an Instagram long-lived token on a channel document."""
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    token_doc = {
        "access_token": body.access_token,
        "token_type": "bearer",
        "expires_at": body.expires_at,
    }

    await db.channels.update_one(
        {"channel_id": channel_id},
        {"$set": {"instagram_tokens": token_doc, "updated_at": now_ist()}},
    )

    mgr = _get_instagram_manager()
    mgr.invalidate(channel_id)

    return {"ok": True, "channel_id": channel_id, "message": "Instagram token stored"}


@router.get("/{channel_id}/instagram-token")
async def get_instagram_token(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return the current Instagram access token, refreshing if needed.

    Unlike YouTube, Instagram uses a single long-lived token (60 days).
    If the token is close to expiry (< 7 days left), it is automatically
    refreshed using the Facebook token-refresh endpoint.
    """
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    tokens = channel.get("instagram_tokens")
    if not tokens:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Instagram tokens stored for channel '{channel_id}'",
        )

    access_token = tokens["access_token"]
    expires_at = tokens.get("expires_at")

    # Auto-refresh if < 7 days remain
    if expires_at:
        from datetime import datetime as dt, timezone as tz, timedelta

        try:
            exp = dt.fromisoformat(expires_at.replace("Z", "+00:00"))
            if exp - dt.now(tz.utc) < timedelta(days=7):
                from app.database import get_instagram_oauth_config
                from app.config import get_settings

                settings = get_settings()
                cfg = await get_instagram_oauth_config(db)
                app_id = (cfg or {}).get("app_id") or settings.INSTAGRAM_APP_ID
                app_secret = (cfg or {}).get("app_secret") or settings.INSTAGRAM_APP_SECRET

                if app_id and app_secret:
                    from app.services.instagram import InstagramService

                    svc = InstagramService(access_token, db=db, channel_id=channel_id)
                    new_token = await svc.refresh_token(app_id, app_secret)
                    if new_token:
                        access_token = new_token
                        from app.timezone import now_ist
                        from datetime import timedelta as td_delta
                        # Estimate new expiry since we don't return it directly from refresh_token
                        expires_at = (now_ist() + td_delta(days=60)).isoformat()
                        mgr = _get_instagram_manager()
                        mgr.invalidate(channel_id)
        except (ValueError, TypeError):
            pass

    return {"ok": True, "access_token": access_token, "expires_at": expires_at}


@router.get("/{channel_id}/instagram-token/status")
async def get_instagram_token_status(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Check Instagram token status without exposing the token value."""
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    tokens = channel.get("instagram_tokens")
    if not tokens:
        return {"channel_id": channel_id, "connected": False, "status": "disconnected"}

    expires_at = tokens.get("expires_at")
    is_expired = False
    if expires_at:
        from datetime import datetime as dt, timezone as tz

        try:
            exp = dt.fromisoformat(expires_at.replace("Z", "+00:00"))
            is_expired = exp <= dt.now(tz.utc)
        except (ValueError, TypeError):
            is_expired = True

    return {
        "channel_id": channel_id,
        "connected": True,
        "status": "expired" if is_expired else "active",
        "expires_at": expires_at,
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