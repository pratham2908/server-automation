"""Analysis router – run analysis updates, retrieve results, compare periods."""

from datetime import datetime
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
from app.timezone import IST

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/analysis",
    tags=["analysis"],
    dependencies=[Depends(verify_api_key)],
)


def _get_services(channel_id: str):
    """Lazy import to avoid circular dependency."""
    from app.main import youtube_service_manager, gemini_service  # type: ignore[import]

    youtube_service = youtube_service_manager.get_service(channel_id) if youtube_service_manager else None
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

    youtube_service, gemini_service = _get_services(channel_id)

    if youtube_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No YouTube token for channel '{channel_id}'. Run: python generate_youtube_token.py {channel_id}",
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
    from app.timezone import now_ist, IST

    doc = await db.analysis.find_one({"channel_id": channel_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No analysis found for channel {channel_id}",
        )
    doc.pop("_id", None)

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
        {"created_at": 1},
    ).to_list(length=None)

    ready_for_analysis = 0
    for v in unanalysed:
        v_created_at = v.get("created_at")
        if not v_created_at:
            ready_for_analysis += 1
            continue
        if v_created_at.tzinfo is None:
            v_created_at = v_created_at.replace(tzinfo=IST)
        if v_created_at <= three_days_ago:
            ready_for_analysis += 1

    not_ready_yet = len(unanalysed) - ready_for_analysis

    return {
        **doc,
        "analysis_status": {
            "ready_for_analysis": ready_for_analysis,
            "not_ready_yet": not_ready_yet,
        },
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
    limit: int = 50,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return per-video analyses for *channel_id*.

    Optional ``from`` and ``to`` query params filter by ``published_at`` (when the
    video was published on YouTube). Both are interpreted in IST. Use ISO 8601–style
    strings: date only (2026-02-08) or datetime (2026-02-08T20 or 2026-02-08T20:00:00).
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

    cursor = (
        db.analysis_history.find(query)
        .sort("published_at", -1)
        .limit(limit)
    )
    results = await cursor.to_list(length=limit)
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
