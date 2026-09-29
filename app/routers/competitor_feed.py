"""Admin-only competitor media feed, for a separate application.

Gated by its own pre-shared secret rather than ``API_KEY``: the consuming
application only needs to read public competitor media, and should not hold a
key that can publish, delete or reconfigure channels. Fails closed — with no
secret configured the feed is off for everyone, not open to everyone.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.config import get_settings
from app.database import get_db
from app.logger import get_logger
from app.services.competitor_feed import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    normalise_post,
    select_source_channel,
    summarise,
    trim_to_new,
)

logger = get_logger(__name__)


async def verify_feed_key(x_feed_key: str = Header(None)) -> str:
    """Authorise a caller against ``COMPETITOR_FEED_KEY``.

    403 rather than 401, matching channel registration: a wrong pre-shared
    secret is not an API-key failure and should not read like one.
    """
    expected = get_settings().COMPETITOR_FEED_KEY
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The competitor feed is disabled: set COMPETITOR_FEED_KEY on the server.",
        )
    if not x_feed_key or not secrets.compare_digest(x_feed_key, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid feed key.")
    return x_feed_key


router = APIRouter(
    prefix="/api/v1/competitor-feed",
    tags=["competitor-feed"],
    dependencies=[Depends(verify_feed_key)],
)


class MediaChild(BaseModel):
    """One frame of a carousel."""

    id: str
    media_type: str
    media_url: str | None = None
    thumbnail_url: str | None = None


class CompetitorPost(BaseModel):
    id: str
    permalink: str
    media_type: str = Field(description="IMAGE | VIDEO | CAROUSEL_ALBUM")
    media_product_type: str = Field(description="FEED | REELS")
    timestamp: str
    caption: str = ""
    comments_count: int = 0
    like_count: int | None = Field(None, description="None means the account hides likes — not the same as zero")
    media_url: str | None = Field(None, description="Directly downloadable. Absent for video: use permalink instead")
    thumbnail_url: str | None = Field(None, description="Poster frame; present for video")
    children: list[MediaChild] = Field(default_factory=list, description="Carousel frames")


class CompetitorProfile(BaseModel):
    instagram_user_id: str | None = None
    username: str
    name: str = ""
    profile_picture_url: str = ""
    followers_count: int = 0
    media_count: int = 0
    biography: str = ""


class CompetitorFeed(BaseModel):
    profile: CompetitorProfile
    posts: list[CompetitorPost]
    returned: int
    media_type_counts: dict[str, int]
    oldest_returned: str | None = Field(None, description="Timestamp of the oldest post in this window")
    history_truncated: bool = Field(
        description="Meta returned fewer posts than the account has. There is no cursor, so the rest is unreachable"
    )
    since_id_found: bool | None = Field(
        None,
        description="None if no marker was sent. False means the marker fell outside the window "
        "and every post is returned, so the caller must de-duplicate",
    )
    source_channel_id: str = Field(description="Which of our channels' tokens ran the query")


async def _load_service(db: AsyncIOMotorDatabase, via_channel_id: str | None) -> tuple[Any, dict[str, Any]]:
    """Resolve the channel whose Facebook-Login token will carry the query."""
    import app.main as main_mod

    channels = await db.channels.find({"platform": "instagram"}).to_list(length=None)
    channel = select_source_channel(channels, via_channel_id)
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"No usable source channel: '{via_channel_id}' cannot run this query"
                if via_channel_id
                else "No Instagram channel with a Facebook-Login token is connected. "
                "business_discovery does not exist on the Instagram Login API."
            ),
        )

    if not main_mod.instagram_service_manager:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Instagram service is unavailable")

    service = await main_mod.instagram_service_manager.get_service(channel["channel_id"])
    if not service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not build an Instagram client for '{channel['channel_id']}'",
        )
    return service, channel


@router.get("/{username}", response_model=CompetitorFeed)
async def get_competitor_feed(
    username: str,
    since_id: str | None = Query(None, description="Return only posts newer than this post id"),
    since_timestamp: str | None = Query(
        None, description="ISO 8601. Alternative to since_id, and survives a deleted marker post"
    ),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    via_channel_id: str | None = Query(None, description="Force a specific source channel's token"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Every post on a public Business/Creator account, newest first.

    Omit both markers for the whole available window; pass one to get only what
    is new. The trimming happens here because business_discovery has no "since"
    filter of its own.
    """
    service, channel = await _load_service(db, via_channel_id)
    target = username.lstrip("@").strip()

    try:
        # The Instagram client is blocking, so it goes off the event loop.
        profile = await asyncio.to_thread(service.discover_business_account, channel["instagram_user_id"], target)
        raw = await asyncio.to_thread(service.discover_all_media, channel["instagram_user_id"], target, limit)
    except ValueError as exc:
        # Raised when the source token is Instagram Login rather than Facebook.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except Exception as exc:
        # Surfaced, not swallowed: the usual causes are a private or personal
        # target and an expired token, and the caller can act on both.
        logger.warning("Competitor feed failed for '%s': %s", target, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Instagram rejected the lookup for '{target}'. "
            "It must be a public Business or Creator account. Underlying error: " + str(exc)[:200],
        )

    posts = [normalise_post(item) for item in raw]
    fetched_total = len(posts)
    posts, since_found = trim_to_new(posts, since_id, since_timestamp)

    return CompetitorFeed(
        profile=CompetitorProfile(**profile),
        posts=[CompetitorPost(**post) for post in posts],
        returned=len(posts),
        media_type_counts=summarise(posts),
        oldest_returned=posts[-1]["timestamp"] if posts else None,
        # Compared against what Meta gave us, not what we then trimmed.
        history_truncated=fetched_total < int(profile.get("media_count", 0)),
        since_id_found=since_found,
        source_channel_id=channel["channel_id"],
    )
