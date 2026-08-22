"""Channel profile mapping — platform account data onto our channel document.

The display name, avatar, and follower counts shown across the app are a cached
copy of what the platform reports. Both the manual refresh endpoint and the daily
growth cron write that copy, so the mapping lives here rather than in either of
them: two copies of these field names would drift the moment one platform adds a
field.

``build_profile_update`` is pure — it takes what a platform client returned and
produces the ``$set`` document. Persisting it is the caller's business.
"""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.timezone import now_ist


def normalise_handle(raw: str) -> str:
    """Return a platform handle in one shape: '@name'.

    YouTube's ``custom_url`` already carries the '@'; Instagram returns a bare
    username. Normalising here means the UI renders one field for both platforms
    instead of branching on platform at every call site.
    """
    handle = raw.strip().lstrip("@")
    return f"@{handle}" if handle else ""


def _set_if_present(update: dict[str, Any], key: str, value: str | None) -> None:
    """Add *key* only when the platform actually returned something.

    A transient empty avatar or handle in a response must not overwrite a good
    stored value with "" — absent means "unknown", not "cleared".
    """
    if value:
        update[key] = value


def build_profile_update(
    platform: str,
    info: dict[str, Any],
    current_name: str = "",
) -> dict[str, Any]:
    """Map a platform's account payload onto channel profile fields.

    *info* is whatever ``YouTubeService.get_channel_info`` or
    ``InstagramService.get_account_info`` returned. *current_name* is kept as a
    last resort so a sparse Instagram response can never blank an existing name.

    Instagram exposes both a display ``name`` and a ``username``; accounts that
    set no display name return an empty string, so username is the fallback.
    """
    if platform == "instagram":
        username = info.get("username") or ""
        update = {
            "name": info.get("name") or username or current_name,
            "description": info.get("biography", ""),
            "subscriber_count": info.get("followers_count", 0),
            "video_count": info.get("media_count", 0),
            "updated_at": now_ist(),
        }
        if username:
            update["handle"] = normalise_handle(username)
            update["instagram_username"] = username
        _set_if_present(update, "thumbnail_url", info.get("profile_picture_url"))
        return update

    update = {
        "name": info.get("name") or current_name,
        "description": info.get("description", ""),
        "subscriber_count": info.get("subscriber_count", 0),
        "video_count": info.get("video_count", 0),
        "view_count": info.get("view_count", 0),
        "updated_at": now_ist(),
    }
    custom_url = info.get("custom_url") or ""
    if custom_url:
        update["custom_url"] = custom_url
        update["handle"] = normalise_handle(custom_url)
    _set_if_present(update, "thumbnail_url", info.get("thumbnail_url"))
    return update


def name_changed(update: dict[str, Any], current_name: str) -> bool:
    """True when *update* renames the channel — worth logging, unlike a count tick."""
    new_name = update.get("name")
    return bool(new_name) and new_name != current_name


async def persist_profile_update(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    update: dict[str, Any],
) -> None:
    """Write a profile update onto the channel document."""
    await db.channels.update_one({"channel_id": channel_id}, {"$set": update})
