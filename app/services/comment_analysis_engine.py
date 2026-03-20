from __future__ import annotations

"""Comment analysis engine -- orchestrates comment fetching, Gemini analysis,
and MongoDB persistence for the automated sentiment/demand extraction system.

Two entry-points:
  ``run_comment_analysis``  -- analyse a single video (fresh or incremental).
  ``run_cron_cycle``        -- full cron pass for one managed channel
                               (own videos + all competitors).
"""

from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.gemini import GeminiService
from app.services.youtube import YouTubeService
from app.services.instagram import InstagramService
from app.timezone import now_ist

logger = get_logger(__name__)

_BATCH_SIZE = 200
_MAX_VIDEOS_PER_RUN = 20
_MAX_COMMENTS_PER_VIDEO = 500
_MIN_COMMENT_WORDS = 3


def _filter_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove very short or empty comments that carry little signal."""
    return [
        c for c in comments
        if len(c.get("text", "").split()) >= _MIN_COMMENT_WORDS
    ]


def _newest_timestamp(comments: list[dict[str, Any]]) -> str | None:
    """Return the most recent ``published_at`` from a list of comments."""
    newest: datetime | None = None
    newest_raw: str | None = None
    for c in comments:
        raw = c.get("published_at", "")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if newest is None or dt > newest:
                newest = dt
                newest_raw = raw
        except (ValueError, AttributeError):
            continue
    return newest_raw


# ------------------------------------------------------------------
# Single-video analysis
# ------------------------------------------------------------------


async def run_comment_analysis(
    db: AsyncIOMotorDatabase,
    gemini_service: GeminiService,
    channel_id: str,
    platform_video_id: str,
    platform: str,
    source: str,
    video_title: str,
    current_comment_count: int,
    *,
    competitor_channel_id: str | None = None,
    youtube_service: YouTubeService | None = None,
    instagram_service: InstagramService | None = None,
    channel_name: str | None = None,
    progress_label: str | None = None,
) -> dict[str, Any] | None:
    """Analyse comments for a single video (fresh or incremental).

    Returns the upserted ``comment_analysis`` document, or ``None`` if
    there was nothing to analyse.
    """

    existing = await db.comment_analysis.find_one({
        "channel_id": channel_id,
        "platform_video_id": platform_video_id,
    })

    is_incremental = existing is not None
    previous_analysis: dict[str, Any] | None = None
    total_previous_comments = 0
    cutoff_timestamp: str | None = None

    if is_incremental:
        previous_analysis = existing.get("analysis")
        total_previous_comments = existing.get("total_comments_analyzed", 0)
        cutoff_raw = existing.get("comments_analyzed_upto")
        if cutoff_raw:
            cutoff_timestamp = (
                cutoff_raw.isoformat() if isinstance(cutoff_raw, datetime) else str(cutoff_raw)
            )

    # ---- Fetch comments ----
    raw_comments: list[dict[str, Any]] = []
    if platform == "youtube" and youtube_service:
        if is_incremental and cutoff_timestamp:
            raw_comments = youtube_service.get_video_comments_since(
                platform_video_id, cutoff_timestamp, max_comments=_MAX_COMMENTS_PER_VIDEO,
            )
        else:
            raw_comments = youtube_service.get_video_comments(
                platform_video_id, max_comments=_MAX_COMMENTS_PER_VIDEO,
            )
    elif platform == "instagram" and instagram_service:
        if is_incremental and cutoff_timestamp:
            raw_comments = instagram_service.get_media_comments_since(
                platform_video_id, cutoff_timestamp,
            )
        else:
            raw_comments = instagram_service.get_media_comments(platform_video_id)

    if not raw_comments:
        if existing and current_comment_count != existing.get("last_known_comment_count"):
            await db.comment_analysis.update_one(
                {"_id": existing["_id"]},
                {"$set": {"last_known_comment_count": current_comment_count, "updated_at": now_ist()}},
            )
        return None

    filtered = _filter_comments(raw_comments)
    if not filtered:
        return None

    newest_ts = _newest_timestamp(raw_comments)

    # ---- Gemini analysis (batched) ----
    analysis_result: dict[str, Any] | None = previous_analysis if is_incremental else None
    batch_prev_count = total_previous_comments

    for i in range(0, len(filtered), _BATCH_SIZE):
        batch = filtered[i: i + _BATCH_SIZE]
        analysis_result = await gemini_service.analyze_comments(
            batch,
            video_title,
            platform,
            previous_analysis=analysis_result,
            total_previous_comments=batch_prev_count,
        )
        batch_prev_count += len(batch)

    if analysis_result is None:
        return None

    # ---- Build video URL ----
    if platform == "youtube":
        video_url = f"https://www.youtube.com/watch?v={platform_video_id}"
    else:
        video_url = f"https://www.instagram.com/reel/{platform_video_id}/"

    # ---- Persist ----
    now = now_ist()

    if is_incremental:
        new_fetched = existing.get("total_comments_fetched", 0) + len(raw_comments)
        new_analyzed = existing.get("total_comments_analyzed", 0) + len(filtered)
        new_version = existing.get("version", 1) + 1

        await db.comment_analysis.update_one(
            {"_id": existing["_id"]},
            {"$set": {
                "total_comments_fetched": new_fetched,
                "total_comments_analyzed": new_analyzed,
                "last_known_comment_count": current_comment_count,
                "comments_analyzed_upto": newest_ts,
                "analysis": analysis_result,
                "analyzed_at": now,
                "version": new_version,
                "updated_at": now,
                "video_title": video_title,
            }},
        )
        logger.info(
            "📝 [%s] [%s] Incremental comment analysis v%d for '%s' (+%d new comments)",
            channel_name or channel_id, progress_label or "1/1",
            new_version, video_title[:50], len(filtered),
        )
    else:
        doc = {
            "channel_id": channel_id,
            "platform_video_id": platform_video_id,
            "platform": platform,
            "source": source,
            "competitor_channel_id": competitor_channel_id,
            "video_title": video_title,
            "video_url": video_url,
            "total_comments_fetched": len(raw_comments),
            "total_comments_analyzed": len(filtered),
            "last_known_comment_count": current_comment_count,
            "comments_analyzed_upto": newest_ts,
            "analysis": analysis_result,
            "analyzed_at": now,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }
        await db.comment_analysis.update_one(
            {"channel_id": channel_id, "platform_video_id": platform_video_id},
            {"$set": doc},
            upsert=True,
        )
        logger.info(
            "📝 [%s] [%s] Fresh comment analysis for '%s' (%d comments)",
            channel_name or channel_id, progress_label or "1/1",
            video_title[:50], len(filtered),
        )

    result = await db.comment_analysis.find_one({
        "channel_id": channel_id,
        "platform_video_id": platform_video_id,
    })
    if result:
        result.pop("_id", None)
    return result


# ------------------------------------------------------------------
# Full cron cycle for one channel
# ------------------------------------------------------------------


async def run_cron_cycle(
    db: AsyncIOMotorDatabase,
    youtube_service_manager: Any,
    instagram_service_manager: Any,
    gemini_service: GeminiService,
    channel_id: str,
    platform: str,
) -> dict[str, Any]:
    """Execute a full comment-analysis pass for *channel_id*.

    1. Discover latest videos from competitors (+ own published videos).
    2. Compare comment counts against stored ``comment_analysis`` docs.
    3. Run fresh or incremental analysis where needed.
    """
    stats = {"analyzed": 0, "re_analyzed": 0, "skipped": 0, "errors": 0}
    videos_to_process: list[dict[str, Any]] = []

    # ---- Get channel name for logging ----
    channel = await db.channels.find_one({"channel_id": channel_id})
    channel_name = channel.get("name", channel_id) if channel else channel_id

    # ---- Competitor videos (YouTube only for now) ----
    if platform == "youtube":
        competitors = await db.competitors.find({"channel_id": channel_id}).to_list(length=None)
        youtube_service: YouTubeService | None = None
        if youtube_service_manager:
            youtube_service = await youtube_service_manager.get_service(channel_id)

        if youtube_service and competitors:
            for comp in competitors:
                comp_yt_id = comp.get("youtube_channel_id")
                if not comp_yt_id:
                    continue
                try:
                    latest = youtube_service.get_channel_latest_videos(comp_yt_id)
                    for v in latest:
                        videos_to_process.append({
                            "platform_video_id": v["video_id"],
                            "platform": "youtube",
                            "source": "competitor",
                            "competitor_channel_id": comp_yt_id,
                            "video_title": v.get("title", ""),
                            "current_comment_count": v.get("comment_count", 0),
                        })
                except Exception as exc:
                    logger.warning(
                        "Failed to fetch latest videos for competitor %s: %s",
                        comp_yt_id, exc,
                    )
                    stats["errors"] += 1

    # ---- Own channel videos (last 30 days only) ----
    from datetime import timedelta
    from app.timezone import UTC
    thirty_days_ago = now_ist() - timedelta(days=30)

    own_videos = await db.videos.find({
        "channel_id": channel_id,
        "status": "published",
        "published_at": {"$gte": thirty_days_ago},
    }).to_list(length=None)

    for v in own_videos:
        if platform == "youtube" and v.get("youtube_video_id"):
            pid = v["youtube_video_id"]
            meta = v.get("metadata") or {}
            videos_to_process.append({
                "platform_video_id": pid,
                "platform": "youtube",
                "source": "own",
                "competitor_channel_id": None,
                "video_title": v.get("title", ""),
                "current_comment_count": meta.get("comments", 0) or 0,
            })
        elif platform == "instagram" and v.get("instagram_media_id"):
            pid = v["instagram_media_id"]
            meta = v.get("metadata") or {}
            videos_to_process.append({
                "platform_video_id": pid,
                "platform": "instagram",
                "source": "own",
                "competitor_channel_id": None,
                "video_title": v.get("title", ""),
                "current_comment_count": meta.get("comments", 0) or 0,
            })

    # ---- Deduplicate by platform_video_id ----
    seen: set[str] = set()
    unique_videos: list[dict[str, Any]] = []
    for vp in videos_to_process:
        key = vp["platform_video_id"]
        if key not in seen:
            seen.add(key)
            unique_videos.append(vp)

    # ---- Decide which need analysis ----
    processed = 0
    total_to_process = min(len(unique_videos), _MAX_VIDEOS_PER_RUN)
    
    for i, vp in enumerate(unique_videos):
        if processed >= _MAX_VIDEOS_PER_RUN:
            break

        existing = await db.comment_analysis.find_one({
            "channel_id": channel_id,
            "platform_video_id": vp["platform_video_id"],
        })

        if existing:
            if vp["current_comment_count"] <= existing.get("last_known_comment_count", 0):
                stats["skipped"] += 1
                continue

        # Need analysis
        yt_svc: YouTubeService | None = None
        ig_svc: InstagramService | None = None
        if vp["platform"] == "youtube" and youtube_service_manager:
            yt_svc = await youtube_service_manager.get_service(channel_id)
        elif vp["platform"] == "instagram" and instagram_service_manager:
            ig_svc = await instagram_service_manager.get_service(channel_id)

        progress_label = f"{processed + 1}/{total_to_process}"
        try:
            result = await run_comment_analysis(
                db=db,
                gemini_service=gemini_service,
                channel_id=channel_id,
                platform_video_id=vp["platform_video_id"],
                platform=vp["platform"],
                source=vp["source"],
                video_title=vp["video_title"],
                current_comment_count=vp["current_comment_count"],
                competitor_channel_id=vp.get("competitor_channel_id"),
                youtube_service=yt_svc,
                instagram_service=ig_svc,
                channel_name=channel_name,
                progress_label=progress_label,
            )
            if result:
                if existing:
                    stats["re_analyzed"] += 1
                else:
                    stats["analyzed"] += 1
            else:
                stats["skipped"] += 1
        except Exception as exc:
            logger.warning(
                "[%s] [%s] Comment analysis failed for video %s: %s",
                channel_name, progress_label, vp["platform_video_id"], exc,
            )
            stats["errors"] += 1

        processed += 1

    logger.info(
        "🔍 Comment analysis cycle for '%s': %d fresh, %d incremental, %d skipped, %d errors",
        channel_id, stats["analyzed"], stats["re_analyzed"],
        stats["skipped"], stats["errors"],
    )
    return stats


# ------------------------------------------------------------------
# Aggregate across all analyses for a channel
# ------------------------------------------------------------------


async def aggregate_comment_analyses(
    db: AsyncIOMotorDatabase,
    channel_id: str,
    source_filter: str | None = None,
    competitor_channel_id: str | None = None,
) -> dict[str, Any]:
    """Combine insights across all analyzed videos into a channel-level summary.

    Merges ``what_audience_loves``, ``complaints``, and ``demands`` across
    all ``comment_analysis`` documents, deduplicating by theme/topic name
    and summing counts.
    """
    query: dict[str, Any] = {"channel_id": channel_id}
    if source_filter:
        query["source"] = source_filter
    if competitor_channel_id:
        query["competitor_channel_id"] = competitor_channel_id

    docs = await db.comment_analysis.find(query).to_list(length=None)
    if not docs:
        return {
            "channel_id": channel_id,
            "total_videos_analyzed": 0,
            "total_comments_analyzed": 0,
            "aggregate_sentiment": {},
            "top_loves": [],
            "top_complaints": [],
            "top_demands": [],
            "all_content_gaps": [],
            "all_trending_topics": [],
            "all_key_insights": [],
        }

    total_comments = sum(d.get("total_comments_analyzed", 0) for d in docs)

    pos_sum = neg_sum = neu_sum = 0.0
    loves: dict[str, dict] = {}
    complaints: dict[str, dict] = {}
    demands: dict[str, dict] = {}
    content_gaps: set[str] = set()
    trending_topics: set[str] = set()
    key_insights: set[str] = set()

    for d in docs:
        analysis = d.get("analysis")
        if not isinstance(analysis, dict):
            logger.warning("Skipping analysis for video %s: 'analysis' field is not a dict (%s)", d.get("platform_video_id"), type(analysis))
            continue
            
        n_comments = d.get("total_comments_analyzed", 1) or 1

        sent = analysis.get("sentiment_summary", {})
        pos_sum += sent.get("positive_percentage", 0) * n_comments
        neg_sum += sent.get("negative_percentage", 0) * n_comments
        neu_sum += sent.get("neutral_percentage", 0) * n_comments

        for signal in analysis.get("what_audience_loves", []):
            key = signal.get("theme", "").lower().strip()
            if not key:
                continue
            if key not in loves:
                loves[key] = {
                    "theme": signal["theme"],
                    "count": 0,
                    "representative_quotes": [],
                }
            loves[key]["count"] += signal.get("count", 1)
            loves[key]["representative_quotes"].extend(
                signal.get("representative_quotes", [])[:2]
            )

        for signal in analysis.get("complaints", []):
            key = signal.get("theme", "").lower().strip()
            if not key:
                continue
            if key not in complaints:
                complaints[key] = {
                    "theme": signal["theme"],
                    "count": 0,
                    "representative_quotes": [],
                }
            complaints[key]["count"] += signal.get("count", 1)
            complaints[key]["representative_quotes"].extend(
                signal.get("representative_quotes", [])[:2]
            )

        for signal in analysis.get("demands", []):
            key = signal.get("topic", "").lower().strip()
            if not key:
                continue
            if key not in demands:
                demands[key] = {
                    "topic": signal["topic"],
                    "demand_type": signal.get("demand_type", "content_request"),
                    "count": 0,
                    "representative_quotes": [],
                }
            demands[key]["count"] += signal.get("count", 1)
            demands[key]["representative_quotes"].extend(
                signal.get("representative_quotes", [])[:2]
            )

        content_gaps.update(analysis.get("content_gaps", []))
        trending_topics.update(analysis.get("trending_topics", []))
        key_insights.update(analysis.get("key_insights", []))

    def _signal_strength(count: int) -> int:
        return min(10, max(1, round(count / max(1, total_comments) * 100)))

    def _build_sorted(items: dict, key_field: str) -> list[dict]:
        result = []
        for item in items.values():
            item["signal_strength"] = _signal_strength(item["count"])
            item["representative_quotes"] = item["representative_quotes"][:4]
            result.append(item)
        result.sort(key=lambda x: x["count"], reverse=True)
        return result[:15]

    overall_pos = pos_sum / total_comments if total_comments else 0
    overall_neg = neg_sum / total_comments if total_comments else 0
    overall_neu = neu_sum / total_comments if total_comments else 0

    if overall_pos > 50:
        overall_label = "positive"
    elif overall_neg > 50:
        overall_label = "negative"
    elif abs(overall_pos - overall_neg) < 15:
        overall_label = "mixed"
    else:
        overall_label = "neutral"

    return {
        "channel_id": channel_id,
        "source_filter": source_filter,
        "total_videos_analyzed": len(docs),
        "total_comments_analyzed": total_comments,
        "aggregate_sentiment": {
            "positive_percentage": round(overall_pos, 1),
            "negative_percentage": round(overall_neg, 1),
            "neutral_percentage": round(overall_neu, 1),
            "overall_sentiment": overall_label,
        },
        "top_loves": _build_sorted(loves, "theme"),
        "top_complaints": _build_sorted(complaints, "theme"),
        "top_demands": _build_sorted(demands, "topic"),
        "all_content_gaps": sorted(content_gaps),
        "all_trending_topics": sorted(trending_topics),
        "all_key_insights": sorted(key_insights),
    }
