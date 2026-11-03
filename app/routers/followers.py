"""Followers by handle, from Instagram's "Download your information" export, one import at a time."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
from app.services import follower_export as fx
from app.timezone import IST, assume_utc, now_ist

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/followers",
    tags=["followers"],
    dependencies=[Depends(verify_api_key)],
)

# The zip Instagram sends for "Followers and following" alone is small; this leaves room for more ticked.
MAX_UPLOAD_BYTES = 150 * 1024 * 1024
SERIES_DAYS = 30
NOT_FOLLOWING_LIMIT = 15


def _iso(value: Any) -> str | None:
    return assume_utc(value).astimezone(IST).isoformat() if isinstance(value, datetime) else None


async def _channel(db: AsyncIOMotorDatabase, channel_id: str) -> dict[str, Any]:
    channel: dict[str, Any] | None = await db.channels.find_one({"channel_id": channel_id})
    if channel is None:
        raise HTTPException(404, "Channel not found")
    if channel.get("platform") != "instagram":
        raise HTTPException(422, "Follower lists are Instagram-only")
    return channel


async def _comment_counts(db: AsyncIOMotorDatabase, channel_id: str) -> Counter[str]:
    rows = await db.comment_log.find({"channel_id": channel_id, "own": {"$ne": True}}, {"author": 1}).to_list(
        length=None
    )
    return Counter(r["author"] for r in rows)


async def _dm_handles(db: AsyncIOMotorDatabase, channel_id: str) -> set[str]:
    rows = await db.dm_conversations.find({"channel_id": channel_id}, {"peer_username": 1}).to_list(length=None)
    return {str(r["peer_username"]).lower() for r in rows if r.get("peer_username")}


@router.post("/import")
async def import_export(
    channel_id: str, file: UploadFile = File(...), db: AsyncIOMotorDatabase = Depends(get_db)
) -> dict[str, Any]:
    """Take one export (the zip, or its followers_1.json) and compare it with the last one imported."""
    channel = await _channel(db, channel_id)
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "That file is too big — request the export with only “Followers and following”.")
    now = now_ist()
    try:
        taken_at = fx.export_taken_at(file.filename or "", channel, now)
        followers = fx.read_export(file.filename or "", content)
        result = await fx.import_snapshot(
            db, channel, followers, taken_at=taken_at, source_name=file.filename or "", now=now
        )
    except fx.ExportError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {**result, "api_followers": channel.get("subscriber_count")}


@router.get("")
async def list_followers(
    channel_id: str,
    status: Literal["following", "unfollowed", "all"] = Query("following"),
    q: str = Query("", max_length=60),
    sort: Literal["recent", "oldest", "comments"] = Query("recent"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """Handles from the imports, with when they followed (or the window they left in), their comments and DMs."""
    await _channel(db, channel_id)
    query: dict[str, Any] = {"channel_id": channel_id}
    if status != "all":
        query["status"] = status
    docs = await db.followers.find(query).to_list(length=None)
    needle = q.strip().lstrip("@").lower()
    if needle:
        docs = [d for d in docs if needle in d["username"]]
    comments = await _comment_counts(db, channel_id)
    dms = await _dm_handles(db, channel_id)
    epoch = datetime.min.replace(tzinfo=IST)

    def when(d: dict[str, Any]) -> datetime:
        stamp = d.get("unfollowed_before") if d.get("status") == fx.STATUS_UNFOLLOWED else d.get("followed_at")
        return assume_utc(stamp) if isinstance(stamp, datetime) else epoch

    if sort == "comments":
        docs.sort(key=lambda d: (comments[d["username"]], when(d)), reverse=True)
    else:
        docs.sort(key=when, reverse=sort == "recent")
    rows = [
        {
            "username": d["username"],
            "status": d.get("status"),
            "followed_at": _iso(d.get("followed_at")),
            "unfollowed_after": _iso(d.get("unfollowed_after")),
            "unfollowed_before": _iso(d.get("unfollowed_before")),
            "likely_day": d.get("likely_day"),
            "comments": comments[d["username"]],
            "dmed": d["username"] in dms,
        }
        for d in docs[offset : offset + limit]
    ]
    return {"total": len(docs), "rows": rows, "next_offset": offset + limit if offset + limit < len(docs) else None}


@router.get("/summary")
async def follower_summary(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    """The last import and what it changed, daily follows/unfollows, and commenters who don't follow."""
    channel = await _channel(db, channel_id)
    latest = await fx.latest_snapshot(db, channel_id)
    now = now_ist()
    first_day = (now - timedelta(days=SERIES_DAYS - 1)).date()
    days = [(first_day + timedelta(days=i)).isoformat() for i in range(SERIES_DAYS)]
    insights = {
        r["date"]: r
        for r in await db.instagram_account_daily.find(
            {"channel_id": channel_id, "date": {"$in": days}}, {"date": 1, "follows": 1, "unfollows": 1}
        ).to_list(length=None)
    }
    followers = await db.followers.find({"channel_id": channel_id}).to_list(length=None)
    followed_by_day = Counter(
        assume_utc(d["followed_at"]).astimezone(IST).date().isoformat()
        for d in followers
        if d.get("status") == fx.STATUS_FOLLOWING and isinstance(d.get("followed_at"), datetime)
    )
    series = [
        {
            "date": day,
            "follows": (insights.get(day) or {}).get("follows"),
            "unfollows": (insights.get(day) or {}).get("unfollows"),
            # From the export: current followers by the day they followed (people who since left aren't in it).
            "followed_still_following": followed_by_day.get(day, 0),
        }
        for day in days
    ]
    base: dict[str, Any] = {
        "has_import": latest is not None,
        "api_followers": channel.get("subscriber_count"),
        "series": series,
    }
    if latest is None:
        return base

    comments = await _comment_counts(db, channel_id)
    dms = await _dm_handles(db, channel_id)
    following = {d["username"] for d in followers if d.get("status") == fx.STATUS_FOLLOWING}
    events = await db.follower_events.find({"channel_id": channel_id, "snapshot_at": latest["taken_at"]}).to_list(
        length=None
    )

    def person(e: dict[str, Any]) -> dict[str, Any]:
        return {
            "username": e["username"],
            "followed_at": _iso(e.get("followed_at")),
            "after": _iso(e.get("after")),
            "before": _iso(e.get("before")),
            "likely_day": e.get("likely_day"),
            "comments": comments[e["username"]],
            "dmed": e["username"] in dms,
        }

    by_kind: dict[str, list[dict[str, Any]]] = {fx.EVENT_FOLLOWED: [], fx.EVENT_UNFOLLOWED: [], fx.EVENT_REFOLLOWED: []}
    for e in events:
        by_kind.setdefault(e["kind"], []).append(person(e))
    not_following = [
        {"username": author, "comments": n, "dmed": author in dms}
        for author, n in comments.most_common()
        if author not in following and not author.startswith("id:")
    ][:NOT_FOLLOWING_LIMIT]
    snapshots = await db.follower_snapshots.count_documents({"channel_id": channel_id})
    return {
        **base,
        "last_import": {"taken_at": _iso(latest["taken_at"]), "count": latest.get("count"), "imports": snapshots},
        "following": len(following),
        "unfollowed_ever": sum(1 for d in followers if d.get("status") == fx.STATUS_UNFOLLOWED),
        "followers_who_comment": sum(1 for h in following if comments[h]),
        "followers_who_dm": len(following & dms),
        "changes": by_kind,
        "commenters_not_following": not_following,
    }
