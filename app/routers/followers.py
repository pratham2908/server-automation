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
from app.services.comment_dm_delivery import record_delivery
from app.services.ig_export import ExportArchive
from app.timezone import IST, assume_utc, now_ist

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/followers",
    tags=["followers"],
    dependencies=[Depends(verify_api_key)],
)

# An export with every category ticked runs to a few hundred MB (nginx takes 500 MB). The zip is read
# member by member from the spooled upload, never into memory whole.
MAX_UPLOAD_BYTES = 480 * 1024 * 1024
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
    """Take one export (the zip in JSON or HTML, or its followers_1 file) and compare it with the last one.

    A zip that holds DMs also says which comment-DMs Instagram refused to deliver (``dm_delivery``).
    """
    channel = await _channel(db, channel_id)
    if (file.size or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "That file is too big — untick photos and videos when requesting the export.")
    now = now_ist()
    name = file.filename or ""
    try:
        head = await file.read(2)
        await file.seek(0)
        if head == b"PK":
            archive = ExportArchive(file.file)
            generated = archive.generated_at()
            taken_at = fx.export_taken_at(name, channel, now, generated)
            followers = archive.followers()
            if followers is None:
                raise fx.ExportError(fx.NO_FOLLOWERS)
            result = await fx.import_snapshot(db, channel, followers, taken_at=taken_at, source_name=name, now=now)
            delivery = await record_delivery(db, channel_id, archive.dm_threads(), generated or taken_at)
            result["dm_delivery"] = delivery if delivery["private_replies_in_export"] else None
        else:
            content = await file.read(fx.MAX_JSON_BYTES + 1)
            if len(content) > fx.MAX_JSON_BYTES:
                raise fx.ExportError("That file is too big to be a followers list — upload the zip.")
            taken_at = fx.export_taken_at(name, channel, now)
            followers = fx.read_export(name, content)
            result = await fx.import_snapshot(db, channel, followers, taken_at=taken_at, source_name=name, now=now)
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
    first_day = (now.astimezone(fx.INSTAGRAM_DAY_TZ) - timedelta(days=SERIES_DAYS - 1)).date()
    days = [(first_day + timedelta(days=i)).isoformat() for i in range(SERIES_DAYS)]
    insights = {
        r["date"]: r
        for r in await db.instagram_account_daily.find(
            {"channel_id": channel_id, "date": {"$in": days}}, {"date": 1, "follows": 1, "unfollows": 1}
        ).to_list(length=None)
    }
    followers = await db.followers.find({"channel_id": channel_id}).to_list(length=None)
    # Bucketed on Instagram's own (Pacific) days, so each bar lines up with that day's insights.
    followed_by_day = Counter(
        fx.instagram_day(d["followed_at"])
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
    window = await _window(db, channel_id, latest, len(by_kind[fx.EVENT_FOLLOWED]), len(by_kind[fx.EVENT_UNFOLLOWED]))
    return {
        **base,
        "last_import": {"taken_at": _iso(latest["taken_at"]), "count": latest.get("count"), "imports": snapshots},
        "following": len(following),
        "unfollowed_ever": sum(1 for d in followers if d.get("status") == fx.STATUS_UNFOLLOWED),
        "followers_who_comment": sum(1 for h in following if comments[h]),
        "followers_who_dm": len(following & dms),
        "changes": by_kind,
        "commenters_not_following": not_following,
        "window": window,
    }


async def _window(
    db: AsyncIOMotorDatabase, channel_id: str, latest: dict[str, Any], new: int, left: int
) -> dict[str, Any] | None:
    """The last two exports' gap, what they found, and what Instagram's daily insights counted over it.

    The insights are whole Pacific days, so the gap is widened to the days it touches; the days
    Instagram hasn't reported yet are counted, so a short total isn't read as a mismatch.
    """
    previous = (
        await db.follower_snapshots.find({"channel_id": channel_id, "taken_at": {"$lt": latest["taken_at"]}})
        .sort("taken_at", -1)
        .limit(1)
        .to_list(1)
    )
    if not previous:
        return None
    after, before = assume_utc(previous[0]["taken_at"]), assume_utc(latest["taken_at"])
    first = datetime.fromisoformat(fx.instagram_day(after)).date()
    last = datetime.fromisoformat(fx.instagram_day(before)).date()
    days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    rows = {
        r["date"]: r
        for r in await db.instagram_account_daily.find(
            {"channel_id": channel_id, "date": {"$in": days}}, {"date": 1, "follows": 1, "unfollows": 1}
        ).to_list(length=None)
    }
    reported = [d for d in days if (rows.get(d) or {}).get("follows") is not None]
    return {
        "after": _iso(after),
        "before": _iso(before),
        "export_new": new,
        "export_unfollowed": left,
        "insight_days": days,
        "insight_days_missing": [d for d in days if d not in reported],
        "api_follows": sum(int(rows[d].get("follows") or 0) for d in reported),
        "api_unfollows": sum(int(rows[d].get("unfollows") or 0) for d in reported),
    }
