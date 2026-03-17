"""Analysis router – run analysis updates, retrieve results, compare periods."""

from datetime import datetime
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger
from app.timezone import IST, UTC

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/analysis",
    tags=["analysis"],
    dependencies=[Depends(verify_api_key)],
)


async def _get_services(channel_id: str):
    """Lazy import to avoid circular dependency."""
    from app.main import youtube_service_manager, gemini_service  # type: ignore[import]

    youtube_service = await youtube_service_manager.get_service(channel_id) if youtube_service_manager else None
    return youtube_service, gemini_service


# ------------------------------------------------------------------
# POST /update  –  run the full analysis pipeline
# ------------------------------------------------------------------


@router.post("/update")
async def run_analysis_update(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Trigger a full analysis update for *channel_id*."""
    from app.services.analysis_engine import run_analysis

    youtube_service, gemini_service = await _get_services(channel_id)

    if youtube_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No YouTube token for channel '{channel_id}'. Store tokens via POST /channels/{channel_id}/youtube-token",
        )
    if gemini_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    result = await run_analysis(
        channel_id, db, youtube_service, gemini_service
    )
    return result


# ------------------------------------------------------------------
# GET /latest  –  retrieve latest analysis
# ------------------------------------------------------------------


@router.get("/latest")
async def get_latest_analysis(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return the latest channel summary for *channel_id*,
    plus counts of videos ready / not yet ready for analysis."""
    from datetime import timedelta
    from app.timezone import now_ist

    # 1. Fetch analysis doc (if any)
    doc = await db.analysis.find_one({"channel_id": channel_id})
    if doc:
        doc.pop("_id", None)
    else:
        doc = {}

    # 2. Calculate analysis_status (ready/unverified/waiting)
    already_analysed_ids: set[str] = set()
    async for h in db.analysis_history.find(
        {"channel_id": channel_id}, {"video_id": 1}
    ):
        already_analysed_ids.add(h["video_id"])

    now = now_ist()
    three_days_ago = now - timedelta(days=3)

    unanalysed = await db.videos.find(
        {
            "channel_id": channel_id,
            "status": "published",
            "video_id": {"$nin": list(already_analysed_ids)},
        },
        {"created_at": 1, "verification_status": 1},
    ).to_list(length=None)

    ready_for_analysis = 0
    unverified = 0
    not_ready_yet = 0

    for v in unanalysed:
        v_created_at = v.get("created_at")
        is_old_enough = False
        if not v_created_at:
            is_old_enough = True
        else:
            if v_created_at.tzinfo is None:
                v_created_at = v_created_at.replace(tzinfo=UTC)
            is_old_enough = v_created_at <= three_days_ago

        if not is_old_enough:
            not_ready_yet += 1
        elif v.get("verification_status") == "unverified":
            unverified += 1
        else:
            ready_for_analysis += 1

    return {
        **doc,
        "analysis_status": {
            "ready_for_analysis": ready_for_analysis,
            "unverified": unverified,
            "not_ready_yet": not_ready_yet,
        },
    }


# ------------------------------------------------------------------
# DELETE /  –  wipe all analysis data for a channel
# ------------------------------------------------------------------


@router.delete("/")
async def delete_analysis(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Delete all analysis data for *channel_id* and reset derived scores.

    Removes the channel summary (``analysis``), all per-video records
    (``analysis_history``), resets every category's score / video_count /
    video_ids / metadata, and zeros out content-param value scores.
    The next ``POST /update`` will re-analyse everything from scratch.
    """
    from app.timezone import now_ist

    # 1. Delete channel summary
    await db.analysis.delete_one({"channel_id": channel_id})

    # 2. Delete all per-video analysis records
    hist_result = await db.analysis_history.delete_many({"channel_id": channel_id})

    # 3. Reset categories: score, video_count, video_ids, metadata
    cat_result = await db.categories.update_many(
        {"channel_id": channel_id},
        {
            "$set": {
                "score": 0,
                "video_count": 0,
                "video_ids": [],
                "metadata": {"total_videos": 0},
                "updated_at": now_ist(),
            }
        },
    )

    # 4. Zero out content_params value scores and video counts
    param_docs = await db.content_params.find(
        {"channel_id": channel_id, "values.0": {"$exists": True}}
    ).to_list(length=None)

    for pdoc in param_docs:
        zeroed = [
            {"value": v["value"], "score": 0, "video_count": 0}
            for v in pdoc["values"]
        ]
        await db.content_params.update_one(
            {"_id": pdoc["_id"]},
            {"$set": {"values": zeroed, "updated_at": now_ist()}},
        )

    logger.success(
        "🗑️ Deleted analysis for channel '%s': %d history records, %d categories reset, %d content params reset",
        channel_id,
        hist_result.deleted_count,
        cat_result.modified_count,
        len(param_docs),
    )

    return {
        "ok": True,
        "channel_id": channel_id,
        "analysis_deleted": True,
        "analysis_history_deleted": hist_result.deleted_count,
        "categories_reset": cat_result.modified_count,
        "content_params_reset": len(param_docs),
    }


def _parse_datetime_ist(value: str) -> datetime:
    """Parse an ISO 8601–style string and return timezone-aware datetime in IST.

    Accepts: YYYY-MM-DD, YYYY-MM-DDTHH, YYYY-MM-DDTHH:MM, YYYY-MM-DDTHH:MM:SS,
    with optional timezone suffix. Naive results are interpreted as IST.
    """
    value = value.strip()
    if not value:
        raise ValueError("Empty date string")
    # Normalise truncated time so fromisoformat accepts it (e.g. 2026-02-08T20 → 2026-02-08T20:00:00)
    if value.count(":") == 0 and "T" in value:
        value = value + ":00:00"
    elif value.count(":") == 1 and "T" in value:
        value = value + ":00"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"Invalid date format: {value!r}. Use ISO 8601 e.g. 2026-02-08 or 2026-02-08T20:00:00"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    else:
        dt = dt.astimezone(IST)
    return dt


# ------------------------------------------------------------------
# GET /history  –  per-video analyses
# ------------------------------------------------------------------

@router.get("/history")
async def get_analysis_history(
    channel_id: str,
    from_date: Optional[str] = Query(None, alias="from", description="Filter published_at >= this (IST). e.g. 2026-02-08 or 2026-02-08T20:00:00"),
    to_date: Optional[str] = Query(None, alias="to", description="Filter published_at <= this (IST). e.g. 2026-02-08 or 2026-02-08T23:59:59"),
    limit: Optional[int] = Query(None, description="Max results; if omitted, returns entire history"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return per-video analyses for *channel_id*.

    Optional ``from`` and ``to`` filter by ``published_at`` (IST). Optional ``limit``
    caps the number of results; if not given, entire history is returned.
    """
    query: dict[str, Any] = {"channel_id": channel_id}

    from_dt: Optional[datetime] = None
    to_dt: Optional[datetime] = None
    if from_date:
        try:
            from_dt = _parse_datetime_ist(from_date)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    if to_date:
        try:
            to_dt = _parse_datetime_ist(to_date)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    if from_dt or to_dt:
        date_filter: dict[str, Any] = {}
        if from_dt:
            date_filter["$gte"] = from_dt
        if to_dt:
            date_filter["$lte"] = to_dt
        query["published_at"] = date_filter

    cursor = db.analysis_history.find(query).sort("published_at", -1)
    if limit is not None:
        cursor = cursor.limit(limit)
    results = await cursor.to_list(length=limit if limit is not None else None)
    for doc in results:
        doc.pop("_id", None)
    return results


# ------------------------------------------------------------------
# GET /history/{video_id}  –  single video analysis
# ------------------------------------------------------------------

@router.get("/history/{video_id}")
async def get_video_analysis(
    channel_id: str,
    video_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return the per-video analysis for a specific video."""
    doc = await db.analysis_history.find_one(
        {"channel_id": channel_id, "video_id": video_id}
    )
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No analysis found for video '{video_id}' in channel '{channel_id}'",
        )
    doc.pop("_id", None)
    return doc


# ------------------------------------------------------------------
# GET /compare  –  time-period comparison
# ------------------------------------------------------------------

@router.get("/compare")
async def compare_periods(
    channel_id: str,
    from1: datetime = Query(..., description="Start of period 1"),
    to1: datetime = Query(..., description="End of period 1"),
    from2: datetime = Query(..., description="Start of period 2"),
    to2: datetime = Query(..., description="End of period 2"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Compare channel performance across two time periods.

    Aggregates per-video analyses from ``analysis_history`` filtered
    by ``analyzed_at`` for each period, returning side-by-side averages.
    """

    async def _aggregate_period(start: datetime, end: datetime) -> dict[str, Any]:
        docs = await db.analysis_history.find(
            {
                "channel_id": channel_id,
                "analyzed_at": {"$gte": start, "$lte": end},
            }
        ).to_list(length=None)

        if not docs:
            return {
                "video_count": 0,
                "avg_views": 0,
                "avg_likes": 0,
                "avg_comments": 0,
                "avg_engagement_rate": 0,
                "avg_percentage_viewed": 0,
                "avg_views_per_subscriber": 0,
                "total_subscribers_gained": 0,
                "avg_performance_rating": 0,
            }

        total = len(docs)

        def _safe_avg(key: str) -> float:
            vals = [d.get("stats_snapshot", {}).get(key, 0) for d in docs]
            return round(sum(vals) / total, 2) if total else 0

        total_subs = sum(
            d.get("stats_snapshot", {}).get("subscribers_gained", 0) for d in docs
        )
        avg_rating = round(
            sum(d.get("ai_insight", {}).get("performance_rating", 0) for d in docs) / total, 1
        )

        return {
            "video_count": total,
            "avg_views": _safe_avg("views"),
            "avg_likes": _safe_avg("likes"),
            "avg_comments": _safe_avg("comments"),
            "avg_engagement_rate": _safe_avg("engagement_rate"),
            "avg_percentage_viewed": _safe_avg("avg_percentage_viewed"),
            "avg_views_per_subscriber": _safe_avg("views_per_subscriber"),
            "total_subscribers_gained": total_subs,
            "avg_performance_rating": avg_rating,
        }

    period1 = await _aggregate_period(from1, to1)
    period2 = await _aggregate_period(from2, to2)

    return {
        "channel_id": channel_id,
        "period_1": {"from": from1, "to": to1, **period1},
        "period_2": {"from": from2, "to": to2, **period2},
    }
