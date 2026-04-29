"""Content Intelligence Engine -- deep video analysis and pattern comparison.

Scans competitor and own-channel videos, extracts hook/CTA/structure
intelligence via Gemini, stores persistently, and generates comparative
insights to surface what's working and what needs to change.
"""

from __future__ import annotations

import uuid
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.gemini import GeminiService
from app.timezone import now_ist

logger = get_logger(__name__)

_BATCH_SIZE = 10
_TOP_N_COMPETITOR_VIDEOS = 30


async def _fetch_competitor_video_metadata(
    channel_id: str,
    db: AsyncIOMotorDatabase,
    youtube_manager: Any,
    instagram_manager: Any,
) -> list[dict[str, Any]]:
    """Fetch top videos from all competitors with full metadata."""
    competitors = await db.competitors.find({"channel_id": channel_id}).to_list(length=None)
    if not competitors:
        return []

    channel = await db.channels.find_one({"channel_id": channel_id})
    channel.get("platform", "youtube") if channel else "youtube"

    all_videos: list[dict[str, Any]] = []

    for comp in competitors:
        platform = comp.get("platform", "youtube")
        comp_name = comp.get("name", "Unknown")

        try:
            if platform == "youtube":
                yt_id = comp.get("youtube_channel_id")
                if not yt_id:
                    continue
                service = await youtube_manager.get_service(channel_id)
                if not service:
                    continue

                raw_vids = service.get_channel_latest_videos(yt_id, max_results=50)
                video_ids = [v["video_id"] for v in raw_vids]
                if not video_ids:
                    continue

                stats_map = service.get_video_stats(video_ids)

                # Fetch snippet details (title, description, tags) in batches
                snippet_map: dict[str, dict] = {}
                for i in range(0, len(video_ids), 50):
                    batch = video_ids[i : i + 50]
                    resp = service._youtube.videos().list(part="snippet", id=",".join(batch)).execute()
                    for item in resp.get("items", []):
                        snip = item.get("snippet", {})
                        snippet_map[item["id"]] = {
                            "description": snip.get("description", ""),
                            "tags": snip.get("tags", []),
                            "title": snip.get("title", ""),
                        }

                for rv in raw_vids:
                    vid = rv["video_id"]
                    st = stats_map.get(vid, {})
                    snip = snippet_map.get(vid, {})
                    all_videos.append(
                        {
                            "platform_video_id": vid,
                            "title": snip.get("title") or rv.get("title", ""),
                            "description": snip.get("description", "")[:1000],
                            "tags": (snip.get("tags") or [])[:20],
                            "views": st.get("views", 0),
                            "likes": st.get("likes", 0),
                            "comments": st.get("comments", 0),
                            "duration_seconds": st.get("duration_seconds", 0),
                            "engagement_rate": st.get("engagement_rate", 0),
                            "published_at": rv.get("published_at", ""),
                            "competitor_name": comp_name,
                            "platform": "youtube",
                            "permalink": f"https://youtube.com/watch?v={vid}",
                        }
                    )

            elif platform == "instagram":
                ig_username = comp.get("instagram_username")
                if not ig_username:
                    continue
                service = await instagram_manager.get_service(channel_id)
                if not service:
                    continue
                own_ig_id = channel.get("instagram_user_id") if channel else None
                if not own_ig_id:
                    continue

                raw_reels = service.discover_competitor_media(own_ig_id, ig_username, max_results=50)
                for rr in raw_reels:
                    caption = rr.get("caption", "") or ""
                    all_videos.append(
                        {
                            "platform_video_id": rr["id"],
                            "title": caption.split("\n")[0][:100] if caption else "Untitled",
                            "description": caption[:1000],
                            "tags": [w.strip("#") for w in caption.split() if w.startswith("#")][:20],
                            "views": rr.get("views", 0),
                            "likes": rr.get("like_count", 0),
                            "comments": rr.get("comment_count", 0),
                            "duration_seconds": 0,
                            "engagement_rate": 0,
                            "published_at": rr.get("published_at", ""),
                            "competitor_name": comp_name,
                            "platform": "instagram",
                            "permalink": rr.get("permalink", ""),
                        }
                    )

        except Exception as exc:
            logger.error(
                "Content intel: failed to fetch competitor '%s' (%s): %s",
                comp_name,
                platform,
                exc,
            )

    # Sort by views descending and take top N per competitor
    by_competitor: dict[str, list] = {}
    for v in all_videos:
        by_competitor.setdefault(v["competitor_name"], []).append(v)

    result = []
    for comp_name, vids in by_competitor.items():
        vids.sort(key=lambda x: x.get("views", 0), reverse=True)
        result.extend(vids[:_TOP_N_COMPETITOR_VIDEOS])

    return result


async def _fetch_own_video_metadata(
    channel_id: str,
    db: AsyncIOMotorDatabase,
) -> list[dict[str, Any]]:
    """Load own published videos with metadata for intelligence extraction."""
    videos = await db.videos.find(
        {"channel_id": channel_id, "status": "published"},
    ).to_list(length=None)

    result = []
    for v in videos:
        meta = v.get("metadata") or {}
        platform_vid = v.get("youtube_video_id") or v.get("instagram_media_id") or ""

        entry = {
            "platform_video_id": platform_vid,
            "title": v.get("title", ""),
            "description": (v.get("description") or "")[:1000],
            "tags": (v.get("tags") or [])[:20],
            "views": meta.get("views", 0),
            "likes": meta.get("likes", 0),
            "comments": meta.get("comments", 0),
            "duration_seconds": meta.get("duration_seconds", 0),
            "engagement_rate": meta.get("engagement_rate", 0),
            "published_at": str(v.get("published_at", "")),
            "category": v.get("category", ""),
            "content_params": v.get("content_params") or {},
            "video_id": v.get("video_id", ""),
        }
        result.append(entry)

    return result


async def _extract_and_store(
    videos: list[dict[str, Any]],
    source: str,
    channel_id: str,
    platform: str,
    db: AsyncIOMotorDatabase,
    gemini_service: GeminiService,
) -> dict[str, Any]:
    """Extract intelligence from videos in batches and store results."""
    # Filter out videos already in the collection
    existing_ids = set()
    async for doc in db.video_intelligence.find(
        {"channel_id": channel_id, "source": source},
        {"platform_video_id": 1},
    ):
        existing_ids.add(doc["platform_video_id"])

    new_videos = [v for v in videos if v.get("platform_video_id") not in existing_ids]
    if not new_videos:
        return {"total": len(videos), "new": 0, "skipped": len(videos)}

    extracted = 0
    failed = 0

    for i in range(0, len(new_videos), _BATCH_SIZE):
        batch = new_videos[i : i + _BATCH_SIZE]
        gemini_input = [
            {
                "video_id": v["platform_video_id"],
                "title": v["title"],
                "description": v.get("description", ""),
                "tags": v.get("tags", []),
                "views": v.get("views", 0),
                "likes": v.get("likes", 0),
                "comments": v.get("comments", 0),
                "duration_seconds": v.get("duration_seconds", 0),
            }
            for v in batch
        ]

        try:
            extractions = await gemini_service.extract_video_intelligence(
                gemini_input,
                platform,
            )
        except Exception as exc:
            logger.error("Gemini extraction failed for batch %d: %s", i // _BATCH_SIZE + 1, exc)
            failed += len(batch)
            continue

        extraction_map = {e["video_id"]: e for e in extractions if "video_id" in e}

        now = now_ist()
        docs_to_insert = []
        for v in batch:
            ext = extraction_map.get(v["platform_video_id"], {})
            doc = {
                "intel_id": str(uuid.uuid4()),
                "channel_id": channel_id,
                "platform_video_id": v["platform_video_id"],
                "source": source,
                "platform": v.get("platform", platform),
                "title": v["title"],
                "description": v.get("description", "")[:500],
                "tags": v.get("tags", []),
                "views": v.get("views", 0),
                "likes": v.get("likes", 0),
                "comments": v.get("comments", 0),
                "duration_seconds": v.get("duration_seconds", 0),
                "engagement_rate": v.get("engagement_rate", 0),
                "published_at": v.get("published_at", ""),
                "permalink": v.get("permalink", ""),
                "competitor_name": v.get("competitor_name"),
                "category": v.get("category"),
                "content_params": v.get("content_params"),
                "video_id": v.get("video_id"),
                "hook_type": ext.get("hook_type"),
                "hook_description": ext.get("hook_description"),
                "cta_type": ext.get("cta_type"),
                "cta_placement": ext.get("cta_placement"),
                "cta_text": ext.get("cta_text"),
                "content_structure": ext.get("content_structure"),
                "content_pacing": ext.get("content_pacing"),
                "key_topics": ext.get("key_topics", []),
                "title_style": ext.get("title_style"),
                "estimated_production": ext.get("estimated_production"),
                "created_at": now,
            }
            docs_to_insert.append(doc)

        if docs_to_insert:
            try:
                await db.video_intelligence.insert_many(docs_to_insert, ordered=False)
                extracted += len(docs_to_insert)
            except Exception as exc:
                logger.warning("Some intel docs may have failed to insert: %s", exc)
                extracted += len(docs_to_insert)

        logger.info(
            "Content intel: extracted batch %d/%d (%s, %d videos)",
            i // _BATCH_SIZE + 1,
            (len(new_videos) + _BATCH_SIZE - 1) // _BATCH_SIZE,
            source,
            len(batch),
        )

    return {
        "total": len(videos),
        "new": extracted,
        "skipped": len(videos) - len(new_videos),
        "failed": failed,
    }


async def scan_competitor_videos(
    channel_id: str,
    db: AsyncIOMotorDatabase,
    gemini_service: GeminiService,
    youtube_manager: Any,
    instagram_manager: Any,
    platform: str = "youtube",
) -> dict[str, Any]:
    """Fetch and analyze competitor videos for content intelligence."""
    logger.info("Content intel: scanning competitor videos for channel '%s'...", channel_id)

    videos = await _fetch_competitor_video_metadata(
        channel_id,
        db,
        youtube_manager,
        instagram_manager,
    )
    if not videos:
        return {"source": "competitor", "total": 0, "new": 0, "skipped": 0}

    result = await _extract_and_store(
        videos,
        "competitor",
        channel_id,
        platform,
        db,
        gemini_service,
    )
    result["source"] = "competitor"
    return result


async def scan_own_videos(
    channel_id: str,
    db: AsyncIOMotorDatabase,
    gemini_service: GeminiService,
    platform: str = "youtube",
) -> dict[str, Any]:
    """Analyze own-channel published videos for content intelligence."""
    logger.info("Content intel: scanning own videos for channel '%s'...", channel_id)

    videos = await _fetch_own_video_metadata(channel_id, db)
    if not videos:
        return {"source": "own", "total": 0, "new": 0, "skipped": 0}

    result = await _extract_and_store(
        videos,
        "own",
        channel_id,
        platform,
        db,
        gemini_service,
    )
    result["source"] = "own"
    return result


async def generate_insights(
    channel_id: str,
    db: AsyncIOMotorDatabase,
    gemini_service: GeminiService,
    platform: str = "youtube",
) -> dict[str, Any]:
    """Generate comparative insights from stored video intelligence."""
    logger.info("Content intel: generating insights for channel '%s'...", channel_id)

    own_docs = await db.video_intelligence.find(
        {"channel_id": channel_id, "source": "own"},
    ).to_list(length=None)

    comp_docs = await db.video_intelligence.find(
        {"channel_id": channel_id, "source": "competitor"},
    ).to_list(length=None)

    if not own_docs and not comp_docs:
        raise ValueError("No video intelligence data found. Run a scan first.")

    def _slim(doc: dict) -> dict:
        """Reduce doc to fields relevant for comparison."""
        return {
            "title": doc.get("title", ""),
            "views": doc.get("views", 0),
            "likes": doc.get("likes", 0),
            "comments": doc.get("comments", 0),
            "engagement_rate": doc.get("engagement_rate", 0),
            "duration_seconds": doc.get("duration_seconds", 0),
            "hook_type": doc.get("hook_type"),
            "hook_description": doc.get("hook_description"),
            "cta_type": doc.get("cta_type"),
            "cta_placement": doc.get("cta_placement"),
            "content_structure": doc.get("content_structure"),
            "content_pacing": doc.get("content_pacing"),
            "title_style": doc.get("title_style"),
            "key_topics": doc.get("key_topics", []),
            "category": doc.get("category"),
            "competitor_name": doc.get("competitor_name"),
        }

    own_slim = [_slim(d) for d in own_docs]
    comp_slim = [_slim(d) for d in comp_docs]

    insights = await gemini_service.compare_content_patterns(
        own_slim,
        comp_slim,
        platform,
    )

    insights["channel_id"] = channel_id
    insights["own_videos_analyzed"] = len(own_docs)
    insights["competitor_videos_analyzed"] = len(comp_docs)

    # Persist the latest insights
    await db.content_insights.replace_one(
        {"channel_id": channel_id},
        {**insights, "updated_at": now_ist()},
        upsert=True,
    )

    return insights
