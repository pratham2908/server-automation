"""Collectors: the owner's questions asked of every comment, and the lists their answers make.

See ``app.services.comment_collectors`` for what a collector is and how answers are counted.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key
from app.services import comment_collector_store as store
from app.services import comment_collectors as cc
from app.timezone import now_ist, parse_ist, to_ist_iso

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/collectors",
    tags=["comment-collectors"],
    dependencies=[Depends(verify_api_key)],
)

MAX_READ_BACK_DAYS = 14


class CollectorCreate(cc.CollectorIn):
    # Also read the comments of the last N days (0: only comments from now on).
    read_back_days: int = Field(default=0, ge=0, le=MAX_READ_BACK_DAYS)


class CollectorTest(BaseModel):
    comment: str = Field(min_length=1, max_length=2200)
    collector: cc.CollectorIn


class DoneIn(BaseModel):
    answer: str = Field(min_length=1, max_length=cc.ANSWER_MAX_LEN)
    done: bool


class MergeIn(BaseModel):
    sources: list[str] = Field(min_length=1, max_length=20)
    into: str = Field(min_length=1, max_length=cc.ANSWER_MAX_LEN)


def _collector_out(c: cc.Collector, counted: int | None = None) -> dict[str, Any]:
    out = c.model_dump(mode="json")
    out["active_from"] = to_ist_iso(c.active_from)
    out["created_at"] = to_ist_iso(c.created_at)
    out["updated_at"] = to_ist_iso(c.updated_at)
    if counted is not None:
        out["comments_counted"] = counted
    return out


async def _channel(db: AsyncIOMotorDatabase, channel_id: str) -> None:
    if not await db.channels.find_one({"channel_id": channel_id}, {"_id": 1}):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Channel not found")


async def _collector(db: AsyncIOMotorDatabase, channel_id: str, collector_id: str) -> cc.Collector:
    found = await store.get_collector(db, channel_id, collector_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Collector not found")
    return found


def _when(value: str | None, name: str) -> datetime | None:
    if not value:
        return None
    try:
        return parse_ist(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"{name} is not a date") from exc


@router.get("")
async def list_collectors(channel_id: str, db: AsyncIOMotorDatabase = Depends(get_db)) -> list[dict[str, Any]]:
    await _channel(db, channel_id)
    out = []
    for c in await store.list_collectors(db, channel_id):
        counted = len(await db.collector_answers.distinct("comment_id", {"collector_id": c.id}))
        out.append(_collector_out(c, counted))
    return out


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_collector(
    channel_id: str, body: CollectorCreate, db: AsyncIOMotorDatabase = Depends(get_db)
) -> dict[str, Any]:
    await _channel(db, channel_id)
    now = now_ist()
    fields = cc.CollectorIn.model_validate(body.model_dump(exclude={"read_back_days"}))
    try:
        created = await store.create_collector(
            db, channel_id, fields, now, active_from=now - timedelta(days=body.read_back_days)
        )
    except store.TooManyCollectorsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _collector_out(created, 0)


@router.patch("/{collector_id}")
async def update_collector(
    channel_id: str, collector_id: str, body: cc.CollectorIn, db: AsyncIOMotorDatabase = Depends(get_db)
) -> dict[str, Any]:
    existing = await _collector(db, channel_id, collector_id)
    return _collector_out(await store.update_collector(db, existing, body, now_ist()))


@router.delete("/{collector_id}")
async def delete_collector(
    channel_id: str, collector_id: str, db: AsyncIOMotorDatabase = Depends(get_db)
) -> dict[str, Any]:
    existing = await _collector(db, channel_id, collector_id)
    return {"ok": True, "answers_removed": await store.delete_collector(db, existing)}


@router.post("/test")
async def test_collector(channel_id: str, body: CollectorTest) -> dict[str, Any]:
    """What a collector (saved or not) would count from one comment. One AI call; nothing is kept."""
    import app.main as main_mod

    gemini = getattr(main_mod, "gemini_service", None)
    if gemini is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="The AI service is unavailable")
    draft = cc.Collector(
        **body.collector.model_dump(),
        id="test",
        channel_id=channel_id,
        active_from=now_ist(),
        created_at=now_ist(),
        updated_at=now_ist(),
    )
    try:
        reply = await gemini.collect_answers(cc.build_prompt(body.comment, [draft]))
    except Exception as exc:  # noqa: BLE001 - shown to the owner as a failed try
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"The AI call failed: {exc}") from exc
    return {"answers": cc.parse_answers(reply, [draft])["test"]}


async def _reels(db: AsyncIOMotorDatabase, channel_id: str, media_ids: list[str]) -> list[dict[str, Any]]:
    videos = await db.videos.find(
        {"channel_id": channel_id, "instagram_media_id": {"$in": media_ids}},
        {"instagram_media_id": 1, "title": 1, "published_at": 1},
    ).to_list(None)
    by_id = {str(v.get("instagram_media_id")): v for v in videos}
    out = [
        {
            "media_id": mid,
            "title": str((by_id.get(mid) or {}).get("title") or "").split("\n")[0][:120],
            "published_at": to_ist_iso((by_id.get(mid) or {}).get("published_at")),
        }
        for mid in media_ids
    ]
    out.sort(key=lambda r: r["published_at"] or "", reverse=True)
    return out


@router.get("/{collector_id}/tally")
async def collector_tally(
    channel_id: str,
    collector_id: str,
    media_id: str | None = Query(None, description="Only this reel's comments"),
    since: str | None = Query(None, description="Comments written on or after this time (ISO; bare is IST)"),
    until: str | None = Query(None, description="Comments written before this time"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """The list: each answer with its votes (a person once per reel), people, comments and likes."""
    collector = await _collector(db, channel_id, collector_id)
    result = await store.tally_for(
        db, collector.id, media_id=media_id, since=_when(since, "since"), until=_when(until, "until")
    )
    # The reel filter offers every reel with answers, whatever the other filters.
    every = await db.collector_answers.distinct("media_id", {"collector_id": collector.id})
    ticked = set(collector.done)
    return {
        **result,
        "entries": [
            {**e, "last_at": to_ist_iso(e["last_at"]), "done": cc.fold(e["answer"]) in ticked} for e in result["entries"]
        ],
        "reels": await _reels(db, channel_id, [str(m) for m in every if m]),
    }


@router.get("/{collector_id}/answers")
async def collector_answers(
    channel_id: str,
    collector_id: str,
    answer: str | None = Query(None, description="Only the comments counted for this answer"),
    media_id: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    sort: Literal["recent", "likes"] = Query("likes", description="Most liked first, or newest first"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> list[dict[str, Any]]:
    collector = await _collector(db, channel_id, collector_id)
    rows = await store.answers_for(
        db,
        collector.id,
        answer=answer,
        media_id=media_id,
        since=_when(since, "since"),
        until=_when(until, "until"),
        limit=limit,
        sort=sort,
    )
    return [
        {
            "comment_id": r.get("comment_id"),
            "media_id": r.get("media_id"),
            "username": r.get("username"),
            "text": r.get("text"),
            "answer": r.get("answer"),
            "like_count": r.get("like_count"),
            "comment_gone": bool(r.get("comment_gone")),
            "commented_at": to_ist_iso(r.get("commented_at")),
        }
        for r in rows
    ]


@router.put("/{collector_id}/done")
async def mark_answer_done(
    channel_id: str, collector_id: str, body: DoneIn, db: AsyncIOMotorDatabase = Depends(get_db)
) -> dict[str, Any]:
    """Tick an answer as dealt with. It keeps its place in the list; only the "most" highlights skip it."""
    collector = await _collector(db, channel_id, collector_id)
    try:
        done = await store.set_answer_done(db, collector, body.answer, body.done, now_ist())
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return {"done": done}


@router.post("/{collector_id}/merge")
async def merge_answers(
    channel_id: str, collector_id: str, body: MergeIn, db: AsyncIOMotorDatabase = Depends(get_db)
) -> dict[str, Any]:
    """Count some answers as another ("NYC" and "new york city" as "New York"), now and from now on."""
    collector = await _collector(db, channel_id, collector_id)
    try:
        merged = await store.merge_answers(db, collector, body.sources, body.into, now_ist())
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return _collector_out(merged)
