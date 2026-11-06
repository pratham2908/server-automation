"""Who follows an Instagram channel, from Instagram's own "Download your information" export.

The API gives only a follower count, so the export is the one exact list of handles. Each import is a
snapshot; comparing it with the previous one says who followed and who left:

- ``follower_snapshots``: one row per imported export (when it was taken, how many it listed).
- ``followers``: one row per handle ever seen, ``following`` or ``unfollowed``. An unfollow is only known
  to have happened between two snapshots (``unfollowed_after`` … ``unfollowed_before``); when the
  account's daily insights show unfollows on exactly one day of that gap, that day is ``likely_day``.
- ``follower_events``: what each import found, so a past import's changes can be shown again.

Someone who follows and leaves between two exports is in neither, so they only show in the daily counts.

Instagram's daily account numbers (follows, unfollows) are counted over **Pacific-time days**, whatever
window is asked for: an export bucketed by Pacific day matches them almost to the follower, while by IST
day it doesn't. So anything compared with those numbers is bucketed with ``instagram_day``.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.timezone import IST, assume_utc

# The clock Instagram's daily insights are counted on.
INSTAGRAM_DAY_TZ = ZoneInfo("America/Los_Angeles")

STATUS_FOLLOWING = "following"
STATUS_UNFOLLOWED = "unfollowed"
EVENT_FOLLOWED = "followed"
EVENT_UNFOLLOWED = "unfollowed"
EVENT_REFOLLOWED = "refollowed"

# A followers export is a few KB per thousand followers; anything far bigger is the wrong download.
MAX_JSON_BYTES = 50 * 1024 * 1024
# Instagram names the zip "instagram-<handle>-<YYYY-MM-DD>-<code>.zip".
ZIP_NAME = re.compile(r"instagram-(?P<handle>[A-Za-z0-9._]+)-(?P<date>\d{4}-\d{2}-\d{2})", re.IGNORECASE)
FOLLOWERS_FILE = re.compile(r"(^|/)followers(_\d+)?\.json$", re.IGNORECASE)


class ExportError(ValueError):
    """The file isn't a followers export we can read; the message says what to do instead."""


def instagram_day(at: datetime) -> str:
    """The date Instagram's daily insights put *at* under (its Pacific-time day)."""
    return assume_utc(at).astimezone(INSTAGRAM_DAY_TZ).date().isoformat()


def _handle_from_href(href: str) -> str:
    path = href.rstrip("/").split("instagram.com/", 1)[-1]
    return path.removeprefix("_u/").split("/")[0].split("?")[0]


def _entries(node: Any) -> Iterator[dict[str, Any]]:
    """Every follower entry, wherever the export version nests them (a bare list or under some key)."""
    if isinstance(node, dict):
        if isinstance(node.get("string_list_data"), list):
            yield node
            return
        for value in node.values():
            yield from _entries(value)
    elif isinstance(node, list):
        for value in node:
            yield from _entries(value)


def parse_followers_json(data: Any) -> dict[str, datetime | None]:
    """Handle (lowercased) → when they followed, from one followers_N.json."""
    out: dict[str, datetime | None] = {}
    for entry in _entries(data):
        item = next((i for i in entry["string_list_data"] if isinstance(i, dict)), {})
        handle = str(item.get("value") or "").strip() or str(entry.get("title") or "").strip()
        if not handle and item.get("href"):
            handle = _handle_from_href(str(item["href"]))
        handle = handle.lstrip("@").lower()
        if not handle:
            continue
        stamp = item.get("timestamp")
        followed = datetime.fromtimestamp(stamp, tz=UTC) if isinstance(stamp, int | float) and stamp > 0 else None
        out[handle] = followed
    return out


NO_FOLLOWERS = "No followers file in this zip. Request the export with “Followers and following” ticked."


def read_export(filename: str, content: bytes) -> dict[str, datetime | None]:
    """The followers in an uploaded export: the whole zip (JSON or HTML), or just its followers_1 file."""
    from app.services.ig_export import ExportArchive, html_offset, parse_followers_html

    if content[:2] == b"PK":
        followers = ExportArchive(io.BytesIO(content)).followers()
        if followers is None:
            raise ExportError(NO_FOLLOWERS)
        return followers
    if filename.lower().endswith(".html"):
        page = content.decode("utf-8", errors="replace")
        shift = html_offset(page)
        if shift is None:
            raise ExportError("Couldn't read the times in this HTML file; upload the whole zip instead.")
        return parse_followers_html(page, shift[0])
    return parse_followers_json(_load(content))


def _load(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExportError("That file isn't JSON — upload the zip Instagram sent, or its followers_1.json.") from exc


def export_taken_at(
    filename: str, channel: dict[str, Any], now: datetime, generated_at: datetime | None = None
) -> datetime:
    """When the export was made: the time its pages say they were generated, else the zip's date;
    a bare JSON counts as made at upload.

    The zip also names the account, which must be this channel's — importing another account's
    followers here would read as everyone unfollowing.
    """
    match = ZIP_NAME.search(filename or "")
    if match:
        own = str(channel.get("instagram_username") or "").lower()
        if own and match["handle"].lower() != own:
            raise ExportError(f"This export is for @{match['handle']}, not @{own}.")
    if generated_at is not None:
        return min(now, generated_at.astimezone(IST))
    if not match:
        return now
    day = datetime.strptime(match["date"], "%Y-%m-%d").date()
    # The zip carries the day, not the hour: the end of that day, and never later than now.
    return min(now, datetime.combine(day, time(23, 59), tzinfo=IST))


async def _likely_day(db: AsyncIOMotorDatabase, channel_id: str, after: datetime, before: datetime) -> str | None:
    """The one Instagram day in (after, before] whose insights show unfollows, if there's exactly one and no gaps."""
    first = assume_utc(after).astimezone(INSTAGRAM_DAY_TZ).date()
    last = assume_utc(before).astimezone(INSTAGRAM_DAY_TZ).date()
    if (last - first).days > 60:
        return None
    days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    rows = await db.instagram_account_daily.find(
        {"channel_id": channel_id, "date": {"$in": days}}, {"date": 1, "unfollows": 1}
    ).to_list(length=None)
    by_day = {r["date"]: r.get("unfollows") for r in rows}
    if any(by_day.get(d) is None for d in days):
        return None
    hits = [d for d in days if (by_day.get(d) or 0) > 0]
    return hits[0] if len(hits) == 1 else None


async def import_snapshot(
    db: AsyncIOMotorDatabase,
    channel: dict[str, Any],
    followers: dict[str, datetime | None],
    *,
    taken_at: datetime,
    source_name: str,
    now: datetime,
) -> dict[str, Any]:
    """Record one export and compare it with the last: new, unfollowed and returning followers."""
    channel_id = str(channel["channel_id"])
    if not followers:
        raise ExportError("The followers list in this export is empty.")
    last = await latest_snapshot(db, channel_id)
    previous_at = assume_utc(last["taken_at"]) if last else None
    if previous_at is not None and taken_at <= previous_at:
        raise ExportError("This export is older than (or the same as) the last one imported, so it can't be compared.")
    first_import = previous_at is None
    existing = {d["username"]: d for d in await db.followers.find({"channel_id": channel_id}).to_list(length=None)}

    new, refollowed, unfollowed = [], [], []
    for handle, followed_at in followers.items():
        doc = existing.get(handle)
        if doc is None:
            await db.followers.insert_one(
                {
                    "channel_id": channel_id,
                    "username": handle,
                    "followed_at": followed_at,
                    "status": STATUS_FOLLOWING,
                    "first_seen_at": taken_at,
                    "last_seen_at": taken_at,
                }
            )
            if not first_import:
                new.append(handle)
        elif doc.get("status") == STATUS_UNFOLLOWED:
            await db.followers.update_one(
                {"channel_id": channel_id, "username": handle},
                {
                    "$set": {"status": STATUS_FOLLOWING, "followed_at": followed_at, "last_seen_at": taken_at},
                    "$unset": {"unfollowed_after": "", "unfollowed_before": "", "likely_day": ""},
                },
            )
            refollowed.append(handle)
    still = [h for h, d in existing.items() if d.get("status") == STATUS_FOLLOWING and h in followers]
    if still:
        await db.followers.update_many(
            {"channel_id": channel_id, "username": {"$in": still}}, {"$set": {"last_seen_at": taken_at}}
        )
    likely = await _likely_day(db, channel_id, previous_at, taken_at) if previous_at else None
    for handle, doc in existing.items():
        if doc.get("status") == STATUS_FOLLOWING and handle not in followers:
            after = assume_utc(doc.get("last_seen_at") or previous_at or taken_at)
            await db.followers.update_one(
                {"channel_id": channel_id, "username": handle},
                {
                    "$set": {
                        "status": STATUS_UNFOLLOWED,
                        "unfollowed_after": after,
                        "unfollowed_before": taken_at,
                        "likely_day": likely,
                    }
                },
            )
            unfollowed.append(handle)

    snapshot = {
        "channel_id": channel_id,
        "taken_at": taken_at,
        "imported_at": now,
        "count": len(followers),
        "source": source_name,
    }
    await db.follower_snapshots.insert_one(snapshot)
    for kind, handles in ((EVENT_FOLLOWED, new), (EVENT_REFOLLOWED, refollowed), (EVENT_UNFOLLOWED, unfollowed)):
        for handle in handles:
            event: dict[str, Any] = {
                "channel_id": channel_id,
                "username": handle,
                "kind": kind,
                "snapshot_at": taken_at,
            }
            if kind == EVENT_UNFOLLOWED:
                event.update(after=previous_at, before=taken_at, likely_day=likely)
            else:
                event["followed_at"] = followers.get(handle)
            await db.follower_events.insert_one(event)
    return {
        "taken_at": taken_at.astimezone(IST).isoformat(),
        "previous_at": previous_at.astimezone(IST).isoformat() if previous_at else None,
        "count": len(followers),
        "first_import": first_import,
        "new": len(new),
        "refollowed": len(refollowed),
        "unfollowed": len(unfollowed),
    }


async def latest_snapshot(db: AsyncIOMotorDatabase, channel_id: str) -> dict[str, Any] | None:
    rows = await db.follower_snapshots.find({"channel_id": channel_id}).sort("taken_at", -1).limit(1).to_list(1)
    return rows[0] if rows else None


async def follower_handles(db: AsyncIOMotorDatabase, channel_id: str) -> set[str] | None:
    """Handles following the channel as of the last import, or None when nothing has been imported."""
    if not await db.follower_snapshots.find_one({"channel_id": channel_id}, {"_id": 1}):
        return None
    rows = await db.followers.find({"channel_id": channel_id, "status": STATUS_FOLLOWING}, {"username": 1}).to_list(
        length=None
    )
    return {r["username"] for r in rows}
