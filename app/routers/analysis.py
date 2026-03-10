"""Analysis router – run analysis updates, retrieve results, compare periods."""

from datetime import datetime
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key

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


# ------------------------------------------------------------------
# GET /history  –  per-video analyses
# ------------------------------------------------------------------

@router.get("/history")
async def get_analysis_history(
    channel_id: str,
    from_date: Optional[datetime] = Query(None, alias="from"),
    to_date: Optional[datetime] = Query(None, alias="to"),
    limit: int = 50,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return per-video analyses for *channel_id*.

    Optional ``from`` and ``to`` query params filter by ``analyzed_at``.
    """
    query: dict[str, Any] = {"channel_id": channel_id}

    if from_date or to_date:
        date_filter: dict[str, Any] = {}
        if from_date:
            date_filter["$gte"] = from_date
        if to_date:
            date_filter["$lte"] = to_date
        query["analyzed_at"] = date_filter

    cursor = (
        db.analysis_history.find(query)
        .sort("analyzed_at", -1)
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
