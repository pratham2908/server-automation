"""Competitor media feed for a separate admin application.

This exists because another application wants to download a competitor's output
and only needs somewhere to get the list. It is deliberately not the existing
competitor path: that one filters to reels and shapes rows for topic discovery,
which for a photo-led account discards nearly everything.

What the caller gets and does not get is dictated by Instagram's
``business_discovery``, and is worth stating plainly since none of it is
negotiable from our side:

* **No view counts, ever.** Not exposed for other people's media.
* **Likes are optional.** An account can hide them, and Meta then omits the
  field entirely — which is why ``like_count`` is ``None`` here rather than 0.
  Zero and hidden are different facts and collapsing them loses the distinction.
* **History has a ceiling.** ``media.limit(n)`` is a maximum, not a page size,
  and no cursor comes back. A 249-post account returned 203 items covering
  about a year; there is no way to reach the rest.
* **Videos carry no media_url.** Photos and carousels do, so they can be
  fetched straight from the response, but a reel offers only a poster frame and
  its permalink.

Incremental fetching is done here rather than at the API: business_discovery has
no "since" filter, so the only honest implementation is to pull the window and
trim it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.logger import get_logger
from app.services.instagram_tokens import PROVIDER_FACEBOOK, ig_user_id_for, provider_of, select_slot
from app.timezone import assume_utc, now_ist

logger = get_logger(__name__)

# A caller polling daily for new posts re-reads the same follower count, name and
# bio every time. Those move slowly, so one Graph call a day per account is
# wasted against a rate budget shared with publishing and syncing. The media list
# is deliberately NOT cached — fetching it is the entire point of the poll.
PROFILE_TTL_HOURS = 24
PROFILE_CACHE_COLLECTION = "competitor_profiles"

# Meta stopped at 203 for a 249-post account; asking for more is free and simply
# returns whatever the ceiling allows that day.
DEFAULT_LIMIT = 250
MAX_LIMIT = 500


def is_business_discovery_capable(channel: dict[str, Any]) -> bool:
    """Whether *channel* can be borrowed to run a business_discovery query.

    Needs a Facebook-Login token (the feature does not exist on the Instagram
    Login API) and our own IG user id to address the query to. Channels stored
    before the provider field existed are Facebook-Login by default, which is
    why a missing provider counts as capable.
    """
    slot = select_slot(channel, PROVIDER_FACEBOOK)
    if channel.get("platform") != "instagram" or not slot:
        return False
    tokens = slot[1]
    return provider_of(tokens) == PROVIDER_FACEBOOK and bool(ig_user_id_for(channel, tokens))


def select_source_channel(channels: list[dict[str, Any]], preferred_id: str | None = None) -> dict[str, Any] | None:
    """Pick the channel whose token will run the query.

    The caller is asking about someone else's account, so which of our channels
    carries the request is an implementation detail — except when the caller
    names one, which is honoured if it is capable. Returns ``None`` when no
    channel can do it at all.
    """
    capable = [c for c in channels if is_business_discovery_capable(c)]
    if preferred_id:
        for channel in capable:
            if channel.get("channel_id") == preferred_id:
                return channel
        return None
    return capable[0] if capable else None


def normalise_post(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one Graph media item into the feed's row.

    ``like_count`` stays ``None`` when absent rather than defaulting to 0: the
    account has hidden likes, which is not the same as having none.
    """
    children = [
        {
            "id": child.get("id", ""),
            "media_type": child.get("media_type", ""),
            "media_url": child.get("media_url"),
            "thumbnail_url": child.get("thumbnail_url"),
        }
        for child in (raw.get("children") or {}).get("data") or []
    ]

    return {
        "id": raw.get("id", ""),
        "permalink": raw.get("permalink", ""),
        "media_type": raw.get("media_type", ""),
        "media_product_type": raw.get("media_product_type", ""),
        "timestamp": raw.get("timestamp", ""),
        "caption": raw.get("caption", ""),
        "comments_count": int(raw.get("comments_count", 0)),
        "like_count": int(raw["like_count"]) if raw.get("like_count") is not None else None,
        "media_url": raw.get("media_url"),
        "thumbnail_url": raw.get("thumbnail_url"),
        "children": children,
    }


def trim_to_new(
    posts: list[dict[str, Any]],
    since_id: str | None = None,
    since_timestamp: str | None = None,
) -> tuple[list[dict[str, Any]], bool | None]:
    """Keep only the posts the caller has not seen.

    Returns ``(posts, since_found)``. ``since_found`` is ``None`` when the caller
    asked for everything, ``True`` when their marker was located, and ``False``
    when it was not — which means their marker is older than the window Meta
    will return, or the post has been deleted. That case returns the whole
    window rather than nothing, because silently returning an empty list would
    read as "no new posts" when the truth is "we cannot tell".

    Media comes back newest-first, so everything before the marker is new.
    """
    if since_id:
        for index, post in enumerate(posts):
            if post.get("id") == since_id:
                return posts[:index], True
        logger.info("since_id %s is outside the returned window — returning all %d posts", since_id, len(posts))
        return posts, False

    if since_timestamp:
        newer = [p for p in posts if (p.get("timestamp") or "") > since_timestamp]
        return newer, True

    return posts, None


def summarise(posts: list[dict[str, Any]]) -> dict[str, int]:
    """Counts per media type, so a caller can see the shape without walking rows."""
    counts: dict[str, int] = {}
    for post in posts:
        key = post.get("media_type") or "UNKNOWN"
        counts[key] = counts.get(key, 0) + 1
    return counts


# --- profile cache ------------------------------------------------------------


def is_profile_fresh(cached: dict[str, Any] | None, now: datetime, ttl_hours: int = PROFILE_TTL_HOURS) -> bool:
    """Whether a cached profile can be served without asking Instagram again.

    ``fetched_at`` comes back from Mongo naive-but-UTC, so it goes through
    ``assume_utc``: relabelling it as IST would shift the instant 5h30m into the
    past and expire a cache entry that is still fresh.
    """
    if not cached:
        return False
    fetched_at = cached.get("fetched_at")
    if not isinstance(fetched_at, datetime):
        return False
    return assume_utc(fetched_at) > now - timedelta(hours=ttl_hours)


async def read_cached_profile(db: Any, username: str) -> dict[str, Any] | None:
    """The stored profile for *username*, fresh or not — freshness is the
    caller's call, because a stale profile still beats failing outright when
    Instagram is the thing that broke."""
    doc = await db[PROFILE_CACHE_COLLECTION].find_one({"username": username})
    return dict(doc) if doc else None


async def write_cached_profile(db: Any, username: str, profile: dict[str, Any]) -> datetime:
    """Store *profile* and return the timestamp recorded against it."""
    fetched_at = now_ist()
    await db[PROFILE_CACHE_COLLECTION].update_one(
        {"username": username},
        {"$set": {"username": username, "profile": profile, "fetched_at": fetched_at}},
        upsert=True,
    )
    return fetched_at
