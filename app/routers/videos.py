"""Videos router – list, status update, queue addition, and YouTube sync."""

import uuid
from datetime import datetime
from typing import Optional

from app.timezone import now_ist

from dateutil.parser import isoparse

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger

logger = get_logger(__name__)
from app.models.video import VideoCreate, VideoStatus, VideoStatusUpdate
from app.services.r2 import R2Service

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/videos",
    tags=["videos"],
    dependencies=[Depends(verify_api_key)],
)


def _get_r2() -> R2Service:
    """Lazy import to avoid circular dependency – replaced at startup."""
    from app.main import r2_service  # type: ignore[import]

    if r2_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="R2 service not initialised",
        )
    return r2_service


# ------------------------------------------------------------------
# GET /  –  video list (with optional suggest_n)
# ------------------------------------------------------------------


from app.models.video import Video

@router.get("/")
async def list_videos(
    channel_id: str,
    status_filter: Optional[str] = None,
    suggest_n: Optional[int] = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return videos for *channel_id*.

    Query params
    ------------
    status : ``todo`` | ``ready`` | ``scheduled`` | ``published`` | ``all`` (default ``all``)
    suggest_n : if provided, mark the top *n* to-do videos as suggested.
    """
    query: dict = {"channel_id": channel_id}
    if status_filter and status_filter != "all":
        query["status"] = status_filter

    # If suggest_n is requested, pick top-N to-do videos (ordered by
    # category score) and flag them.
    if suggest_n and suggest_n > 0:
        # Reset previous suggestions for this channel.
        await db.videos.update_many(
            {"channel_id": channel_id, "suggested": True},
            {"$set": {"suggested": False, "updated_at": now_ist()}},
        )

        # Fetch active categories sorted by score to determine priority.
        categories = (
            await db.categories.find(
                {"channel_id": channel_id, "status": "active"}
            )
            .sort("score", -1)
            .to_list(length=None)
        )
        cat_order = {c["name"]: idx for idx, c in enumerate(categories)}

        todo_videos = await db.videos.find(
            {"channel_id": channel_id, "status": "todo"}
        ).to_list(length=None)

        # Sort by category score (best first).
        todo_videos.sort(key=lambda v: cat_order.get(v.get("category", ""), 9999))

        for v in todo_videos[:suggest_n]:
            await db.videos.update_one(
                {"_id": v["_id"]},
                {"$set": {"suggested": True, "updated_at": now_ist()}},
            )

    videos = await db.videos.find(query).to_list(length=None)

    # Strip Mongo _id for JSON serialisation.
    for v in videos:
        v.pop("_id", None)

    # Build sync status: compare YouTube video count vs DB published count.
    sync_status = await _get_sync_status(channel_id, db)

    return {
        "videos": videos,
        "sync_status": sync_status,
    }


def _fetch_youtube_video_ids(yt, youtube_channel_id: str) -> list[str]:
    """Fetch only the video IDs from a channel's uploads playlist (no metadata)."""
    uploads_playlist_id = "UU" + youtube_channel_id[2:]

    video_ids: list[str] = []
    next_page = None

    while True:
        request = yt._youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=next_page,
        )
        response = request.execute()
        for item in response.get("items", []):
            video_ids.append(item["contentDetails"]["videoId"])
        next_page = response.get("nextPageToken")
        if not next_page:
            break

    return video_ids


def _check_youtube_live_status(yt, youtube_video_ids: list[str]) -> dict[str, dict]:
    """Check which videos are live (public) on YouTube.

    Returns a dict mapping youtube_video_id → {\"live\": bool, \"published_at\": str | None}
    for each ID that is publicly visible.
    """
    from app.timezone import IST

    result: dict[str, dict] = {}
    now = now_ist()

    for i in range(0, len(youtube_video_ids), 50):
        batch = youtube_video_ids[i : i + 50]
        resp = (
            yt._youtube.videos()
            .list(part="status,snippet", id=",".join(batch))
            .execute()
        )
        for item in resp.get("items", []):
            vid_id = item["id"]
            privacy = item.get("status", {}).get("privacyStatus", "")
            published_at_str = item.get("snippet", {}).get("publishedAt")

            is_live = privacy == "public"

            published_at_dt = None
            if published_at_str:
                try:
                    published_at_dt = isoparse(published_at_str).astimezone(IST)
                    if published_at_dt > now:
                        is_live = False
                except (ValueError, TypeError):
                    pass

            result[vid_id] = {
                "live": is_live,
                "published_at": published_at_dt,
            }

    return result


async def _get_sync_status(channel_id: str, db) -> dict:
    """Compare YouTube video IDs against our DB to get accurate sync numbers."""
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel or not channel.get("youtube_channel_id"):
        return {"available": False, "reason": "No YouTube channel linked"}

    youtube_service, _ = _get_services(channel_id)
    if youtube_service is None:
        return {"available": False, "reason": "No YouTube token for this channel"}

    try:
        yt_video_ids = set(
            _fetch_youtube_video_ids(youtube_service, channel["youtube_channel_id"])
        )
    except Exception as exc:
        logger.warning("Could not fetch YouTube video IDs for sync status: %s", exc)
        return {"available": False, "reason": "Failed to reach YouTube API"}

    db_yt_ids: set[str] = set()
    async for doc in db.videos.find(
        {"channel_id": channel_id, "youtube_video_id": {"$ne": None}},
        {"youtube_video_id": 1},
    ):
        db_yt_ids.add(doc["youtube_video_id"])

    new_videos_to_import = yt_video_ids - db_yt_ids
    metadata_to_refresh = yt_video_ids & db_yt_ids

    # Check how many scheduled videos are actually live on YouTube.
    scheduled_docs = await db.videos.find(
        {
            "channel_id": channel_id,
            "status": "scheduled",
            "youtube_video_id": {"$ne": None},
        },
        {"youtube_video_id": 1},
    ).to_list(length=None)

    pending_reconciliation = 0
    if scheduled_docs:
        scheduled_yt_ids = [d["youtube_video_id"] for d in scheduled_docs]
        try:
            live_status = _check_youtube_live_status(youtube_service, scheduled_yt_ids)
            pending_reconciliation = sum(
                1 for info in live_status.values() if info["live"]
            )
        except Exception as exc:
            logger.warning("Could not check live status for scheduled videos: %s", exc)

    return {
        "available": True,
        "youtube_total": len(yt_video_ids),
        "in_database": len(db_yt_ids),
        "new_videos_to_import": len(new_videos_to_import),
        "pending_reconciliation": pending_reconciliation,
        "metadata_to_refresh": len(metadata_to_refresh),
    }


# ------------------------------------------------------------------
# PATCH /{video_id}/status  –  mark done / todo
# ------------------------------------------------------------------


@router.patch("/{video_id}/status")
async def update_video_status(
    channel_id: str,
    video_id: str,
    body: VideoStatusUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Toggle video status between ``done`` and ``todo``."""
    update_fields = {
        "status": body.status.value,
        "updated_at": now_ist(),
    }
    if body.status == VideoStatus.PUBLISHED:
        update_fields["published_at"] = now_ist()

    result = await db.videos.update_one(
        {"channel_id": channel_id, "video_id": video_id},
        {"$set": update_fields},
    )
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video {video_id} not found",
        )

    # Update category video count when marking published.
    if body.status == VideoStatus.PUBLISHED:
        video = await db.videos.find_one(
            {"channel_id": channel_id, "video_id": video_id}
        )
        if video and video.get("category"):
            await db.categories.update_one(
                {"channel_id": channel_id, "name": video["category"]},
                {"$inc": {"video_count": 1}},
            )

    return {"ok": True, "video_id": video_id, "status": body.status.value}


# ------------------------------------------------------------------
# POST /upload  –  upload video file to R2 and mark ready
# ------------------------------------------------------------------


@router.post("/{video_id}/upload", status_code=status.HTTP_201_CREATED)
async def upload_video(
    channel_id: str,
    video_id: str,
    file: UploadFile = File(...),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Upload a video file for an existing ``todo`` video and add it to the ready queue.

    The video file is streamed to R2 storage.
    """
    # Verify video exists and is in todo state
    video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video {video_id} not found",
        )
    if video.get("status") not in ("todo",):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Video must be in 'todo' status to upload (current: {video.get('status')})",
        )

    r2 = _get_r2()
    r2_key = f"{channel_id}/{video_id}.mp4"

    # Stream file to R2.
    r2.upload_video(file.file, r2_key)

    now = now_ist()

    # Update video document.
    await db.videos.update_one(
        {"channel_id": channel_id, "video_id": video_id},
        {
            "$set": {
                "status": "ready",
                "r2_object_key": r2_key,
                "updated_at": now,
            }
        }
    )

    # Determine next position in queue.
    last = await db.posting_queue.find_one(
        {"channel_id": channel_id},
        sort=[("position", -1)],
    )
    next_pos = (last["position"] + 1) if last else 1

    await db.posting_queue.insert_one(
        {
            "channel_id": channel_id,
            "video_id": video_id,
            "position": next_pos,
            "added_at": now,
        }
    )

    # Fetch updated document to return
    updated_video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if updated_video:
        updated_video.pop("_id", None)
        
    return {"ok": True, "video": updated_video, "queue_position": next_pos}


# ------------------------------------------------------------------
# POST /{video_id}/schedule  –  schedule video(s) on YouTube
# ------------------------------------------------------------------


@router.post("/{video_id}/schedule")
async def schedule_video(
    channel_id: str,
    video_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Schedule video(s) from the **ready queue** on YouTube.

    - Pass a specific ``video_id`` to schedule one video.
    - Pass ``"all"`` as ``video_id`` to schedule every video in the ready queue.

    For each video the operation:
    1. Computes a publish slot from the channel's ``best_posting_times``.
    2. Downloads the file from R2.
    3. Uploads to YouTube as private with ``publishAt``.
    4. **Only on success**: removes from the ready queue, inserts into
       the scheduled queue, sets status to ``scheduled``.
    """
    from app.config import get_settings
    from app.services.scheduler import compute_schedule_slots
    from app.services.schedule_operation import schedule_single_video

    settings = get_settings()
    r2_service = _get_r2()
    youtube_service, _ = _get_services(channel_id)

    if youtube_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No YouTube token for channel '{channel_id}'. Run: python generate_youtube_token.py {channel_id}",
        )

    # ---- Collect the videos to schedule ----
    if video_id.lower() == "all":
        posting_entries = (
            await db.posting_queue.find({"channel_id": channel_id})
            .sort("position", 1)
            .to_list(length=None)
        )
        if not posting_entries:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No videos in the ready queue to schedule",
            )
        videos_to_schedule = []
        for entry in posting_entries:
            v = await db.videos.find_one(
                {"channel_id": channel_id, "video_id": entry["video_id"]}
            )
            if v and v.get("status") == "ready":
                videos_to_schedule.append(v)
    else:
        video = await db.videos.find_one(
            {"channel_id": channel_id, "video_id": video_id}
        )
        if not video:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Video {video_id} not found",
            )
        if video.get("status") != "ready":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Video must be in 'ready' status to schedule (current: {video.get('status')})",
            )
        videos_to_schedule = [video]

    if not videos_to_schedule:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No ready videos found to schedule",
        )

    # ---- Fetch best_posting_times from the latest analysis ----
    analysis = await db.analysis.find_one({"channel_id": channel_id})
    if not analysis or not analysis.get("best_posting_times"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No analysis with best_posting_times found — run analysis first",
        )

    # ---- Gather already-occupied slots from existing scheduled queue ----
    existing_scheduled = await db.schedule_queue.find(
        {"channel_id": channel_id}
    ).to_list(length=None)
    occupied_datetimes = [e.get("scheduled_at") for e in existing_scheduled]

    # ---- Compute publish slots ----
    slots = compute_schedule_slots(
        best_posting_times=analysis["best_posting_times"],
        occupied_datetimes=occupied_datetimes,
        num_videos=len(videos_to_schedule),
        timezone_str=settings.TIMEZONE,
    )

    if len(slots) < len(videos_to_schedule):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could only find {len(slots)} available slots for {len(videos_to_schedule)} videos — not enough posting slots in analysis",
        )

    # ---- Schedule each video (upload to YouTube) ----
    results = []
    scheduled = 0
    failed = 0

    for video_doc, slot_dt in zip(videos_to_schedule, slots):
        result = await schedule_single_video(
            db=db,
            r2_service=r2_service,
            youtube_service=youtube_service,
            channel_id=channel_id,
            video_doc=video_doc,
            scheduled_at=slot_dt,
        )
        results.append(result)
        if result["status"] == "scheduled":
            scheduled += 1
        else:
            failed += 1

    return {
        "ok": True,
        "scheduled": scheduled,
        "failed": failed,
        "videos": results,
    }


# ------------------------------------------------------------------
# POST /sync  –  sync videos from YouTube + categorise via Gemini
# ------------------------------------------------------------------


def _get_services(channel_id: str):
    """Lazy import to avoid circular dependency."""
    from app.main import youtube_service_manager, gemini_service  # type: ignore[import]

    youtube_service = youtube_service_manager.get_service(channel_id) if youtube_service_manager else None
    return youtube_service, gemini_service


def _parse_iso8601_duration(duration: str) -> int:
    """Convert ISO 8601 duration (e.g. 'PT1H2M30S') to total seconds."""
    import re

    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _compute_rates(views: int, likes: int, comments: int) -> dict:
    """Derive engagement, like, and comment rates from raw counts."""
    if views > 0:
        return {
            "engagement_rate": round((likes + comments) / views * 100, 4),
            "like_rate": round(likes / views * 100, 4),
            "comment_rate": round(comments / views * 100, 4),
        }
    return {"engagement_rate": None, "like_rate": None, "comment_rate": None}


def _fetch_all_youtube_videos(yt, youtube_channel_id: str):
    """Fetch every video from a channel's uploads playlist."""
    uploads_playlist_id = "UU" + youtube_channel_id[2:]

    video_ids = []
    next_page = None

    while True:
        request = yt._youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=next_page,
        )
        response = request.execute()
        for item in response.get("items", []):
            video_ids.append(item["contentDetails"]["videoId"])
        next_page = response.get("nextPageToken")
        if not next_page:
            break

    videos = []
    for i in range(0, len(video_ids), 50):
        batch_ids = video_ids[i : i + 50]
        resp = (
            yt._youtube.videos()
            .list(
                part="snippet,statistics,contentDetails",
                id=",".join(batch_ids),
            )
            .execute()
        )
        for item in resp.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})

            views = int(stats.get("viewCount", 0))
            likes = int(stats.get("likeCount", 0))
            comments = int(stats.get("commentCount", 0))
            duration_seconds = _parse_iso8601_duration(content.get("duration"))
            rates = _compute_rates(views, likes, comments)

            videos.append(
                {
                    "youtube_video_id": item["id"],
                    "title": snippet.get("title", ""),
                    "description": snippet.get("description", ""),
                    "tags": snippet.get("tags", []),
                    "published_at": snippet.get("publishedAt", ""),
                    "views": views,
                    "likes": likes,
                    "comments": comments,
                    "duration_seconds": duration_seconds,
                    **rates,
                }
            )

    return videos


async def _categorize_batch(gemini_service, channel_description, existing_categories, batch, category_instructions=""):
    """Ask Gemini to assign category + topic per video."""
    import json

    video_summaries = [
        {
            "youtube_video_id": v["youtube_video_id"],
            "title": v["title"],
            "description": v["description"][:500],
            "tags": v["tags"][:15],
        }
        for v in batch
    ]

    cats_section = ""
    if existing_categories:
        cats_section = (
            f"\n\n## Existing Categories\n"
            f"Reuse these when a video fits: {json.dumps(existing_categories)}\n"
            f"Only create a new category if NONE of the above fit."
        )

    prompt = f"""You are a YouTube channel analyst. Categorize these videos.

## Channel Description
{channel_description}
{cats_section}

## Videos
```json
{json.dumps(video_summaries, indent=2)}
```

{f"## Additional Instructions for Categorization" + chr(10) + category_instructions if category_instructions else ""}
For each video, return a JSON array:
[{{"youtube_video_id": "...", "category": "...", "topic": "..."}}]

Reuse existing categories. Only create new ones if truly needed."""

    response_text = await gemini_service._generate(prompt)

    try:
        return json.loads(response_text)
    except (json.JSONDecodeError, TypeError):
        return []


class SyncRequest(BaseModel):
    """Optional body for the sync endpoint."""
    new_category_description: Optional[str] = Field(
        None,
        description="Extra instructions for Gemini on how to categorize videos",
    )


@router.post("/sync")
async def sync_videos(
    channel_id: str,
    body: Optional[SyncRequest] = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Sync videos from YouTube into the DB.

    Fetches all videos from the YouTube channel, finds any that aren't
    already in the ``videos`` collection, categorises them via Gemini,
    and inserts them as ``done``.
    """
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

    # Look up channel.
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    youtube_channel_id = channel["youtube_channel_id"]
    channel_description = channel.get("description", "")

    # Fetch all YouTube videos (Data API: snippet + statistics + contentDetails).
    all_yt_videos = _fetch_all_youtube_videos(youtube_service, youtube_channel_id)

    # Enrich with Analytics API data (avg_percentage_viewed, avg_view_duration, etc.)
    all_yt_ids = [v["youtube_video_id"] for v in all_yt_videos]
    analytics_data = youtube_service.get_video_analytics(all_yt_ids)
    for v in all_yt_videos:
        extra = analytics_data.get(v["youtube_video_id"], {})
        v.update(extra)

    # Build a lookup from youtube_video_id → fresh stats for quick access.
    yt_stats_lookup = {v["youtube_video_id"]: v for v in all_yt_videos}

    # Find already-imported youtube_video_ids and refresh their metadata.
    existing_yt_ids = set()
    metadata_updated = 0
    async for doc in db.videos.find(
        {"channel_id": channel_id, "youtube_video_id": {"$ne": None}},
        {"youtube_video_id": 1},
    ):
        yt_id = doc["youtube_video_id"]
        existing_yt_ids.add(yt_id)

        fresh = yt_stats_lookup.get(yt_id)
        if not fresh:
            continue

        await db.videos.update_one(
            {"_id": doc["_id"]},
            {
                "$set": {
                    "metadata.views": fresh.get("views"),
                    "metadata.likes": fresh.get("likes"),
                    "metadata.comments": fresh.get("comments"),
                    "metadata.duration_seconds": fresh.get("duration_seconds"),
                    "metadata.engagement_rate": fresh.get("engagement_rate"),
                    "metadata.like_rate": fresh.get("like_rate"),
                    "metadata.comment_rate": fresh.get("comment_rate"),
                    "metadata.avg_percentage_viewed": fresh.get("avg_percentage_viewed"),
                    "metadata.avg_view_duration_seconds": fresh.get("avg_view_duration_seconds"),
                    "metadata.estimated_minutes_watched": fresh.get("estimated_minutes_watched"),
                    "updated_at": now_ist(),
                }
            },
        )
        metadata_updated += 1

    if metadata_updated:
        logger.success(f"Refreshed metadata for {metadata_updated} existing video(s).")

    # ------------------------------------------------------------------
    # Reconcile: find videos marked "scheduled" in the DB and check if
    # they are actually live (public) on YouTube.
    # Runs before the new-video check so it always executes.
    # ------------------------------------------------------------------
    now = now_ist()
    scheduled_videos = await db.videos.find(
        {
            "channel_id": channel_id,
            "status": "scheduled",
            "youtube_video_id": {"$ne": None},
        }
    ).to_list(length=None)

    reconciled = 0

    if scheduled_videos:
        scheduled_yt_ids = [v["youtube_video_id"] for v in scheduled_videos]
        live_status = _check_youtube_live_status(youtube_service, scheduled_yt_ids)

        for vid in scheduled_videos:
            yt_id = vid["youtube_video_id"]
            info = live_status.get(yt_id)
            if not info or not info["live"]:
                continue

            published_at = info.get("published_at") or vid.get("scheduled_at") or now

            await db.videos.update_one(
                {"_id": vid["_id"]},
                {
                    "$set": {
                        "status": "published",
                        "published_at": published_at,
                        "updated_at": now,
                    }
                },
            )
            await db.schedule_queue.delete_one(
                {"channel_id": channel_id, "video_id": vid["video_id"]}
            )
            reconciled += 1
            logger.info(
                "Reconciled scheduled video '%s' — now live on YouTube.",
                vid.get("title", vid["video_id"]),
            )

    if reconciled:
        logger.success(f"Reconciled {reconciled} scheduled video(s) as published.")

    new_videos = [
        v for v in all_yt_videos if v["youtube_video_id"] not in existing_yt_ids
    ]
    
    logger.info(
        "Found %d total videos on channel. %d existing (metadata refreshed), %d new to process.",
        len(all_yt_videos),
        len(existing_yt_ids),
        len(new_videos),
        extra={"color": "BLUE"},
    )

    if not new_videos:
        return {
            "ok": True,
            "synced": 0,
            "reconciled": reconciled,
            "metadata_refreshed": metadata_updated,
            "message": f"All {len(all_yt_videos)} videos already in DB — metadata refreshed",
            "videos": [],
        }

    # Build running category list.
    existing_cats = [
        c["name"]
        async for c in db.categories.find(
            {"channel_id": channel_id}, {"name": 1}
        )
    ]

    # Categorize in batches of 5.
    BATCH_SIZE = 5
    categorizations = {}

    for i in range(0, len(new_videos), BATCH_SIZE):
        batch = new_videos[i : i + BATCH_SIZE]
        
        logger.info(
            "Asking Gemini to categorize batch (%d/%d)...",
            (i // BATCH_SIZE) + 1,
            (len(new_videos) + BATCH_SIZE - 1) // BATCH_SIZE,
            extra={"color": "MAGENTA"},
        )
        
        results = await _categorize_batch(
            gemini_service, channel_description, existing_cats, batch,
            category_instructions=body.new_category_description if body else "",
        )

        for r in results:
            yt_id = r.get("youtube_video_id", "")
            cat = r.get("category", "Uncategorized")
            topic = r.get("topic", "")
            categorizations[yt_id] = {"category": cat, "topic": topic}

            # Auto-create new category.
            if cat not in existing_cats:
                existing_cats.append(cat)
                now = now_ist()
                await db.categories.insert_one(
                    {
                        "channel_id": channel_id,
                        "name": cat,
                        "description": "",
                        "raw_description": "",
                        "score": 0,
                        "status": "active",
                        "video_count": 0,
                        "metadata": {"total_videos": 0},
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                logger.success(f"Created new category: '{cat}'")

    # ------------------------------------------------------------------
    # Insert newly discovered videos.
    # ------------------------------------------------------------------
    docs = []
    for v in new_videos:
        yt_id = v["youtube_video_id"]
        cat_info = categorizations.get(yt_id, {"category": "Uncategorized", "topic": ""})
        now = now_ist()

        # Use YouTube publish date as both created_at and published_at.
        yt_published_at = now
        if v.get("published_at"):
            try:
                from app.timezone import IST
                yt_published_at = isoparse(v["published_at"]).astimezone(IST)
            except (ValueError, TypeError):
                pass

        docs.append(
            {
                "channel_id": channel_id,
                "video_id": str(uuid.uuid4()),
                "title": v["title"],
                "description": v["description"],
                "tags": v["tags"],
                "category": cat_info["category"],
                "topic": cat_info["topic"],
                "status": "published",
                "suggested": False,
                "basis_factor": "Synced from YouTube",
                "youtube_video_id": yt_id,
                "r2_object_key": None,
                "metadata": {
                    "views": v.get("views"),
                    "likes": v.get("likes"),
                    "comments": v.get("comments"),
                    "duration_seconds": v.get("duration_seconds"),
                    "engagement_rate": v.get("engagement_rate"),
                    "like_rate": v.get("like_rate"),
                    "comment_rate": v.get("comment_rate"),
                    "avg_percentage_viewed": v.get("avg_percentage_viewed"),
                    "avg_view_duration_seconds": v.get("avg_view_duration_seconds"),
                    "estimated_minutes_watched": v.get("estimated_minutes_watched"),
                },
                "published_at": yt_published_at,
                "created_at": yt_published_at,
                "updated_at": now,
            }
        )

    if docs:
        await db.videos.insert_many(docs)
        logger.success(f"Inserted {len(docs)} new synchronized videos into database")

    # Build per-video summary.
    video_summary = [
        {"title": d["title"], "category": d["category"], "topic": d["topic"]}
        for d in docs
    ]
    
    new_cats_count = len(existing_cats) - len(categories_before_sync) if 'categories_before_sync' in locals() else 0
    # Calculate created categories cleanly
    logger.success(
        f"✅ YouTube Sync Complete! Synced {len(docs)} new videos.",
        extra={"color": "BRIGHT_GREEN"}
    )

    return {
        "ok": True,
        "synced": len(docs),
        "reconciled": reconciled,
        "metadata_refreshed": metadata_updated,
        "categories_created": [
            c for c in existing_cats
        ],
        "videos": video_summary,
    }


# ------------------------------------------------------------------
# POST /updateToDoList  –  generate n new videos based on latest analysis
# ------------------------------------------------------------------

class TodoGenerateRequest(BaseModel):
    n: int = Field(gt=0, description="The number of videos to generate")


@router.post("/updateToDoList")
async def generate_todos(
    channel_id: str,
    body: TodoGenerateRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Generate `n` new to-do videos for *channel_id*."""
    from app.services.todo_engine import generate_todo_videos

    _, gemini_service = _get_services(channel_id)

    if gemini_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    await generate_todo_videos(
        channel_id=channel_id,
        target_count=body.n,
        db=db,
        gemini_service=gemini_service,
    )

    return {
        "ok": True,
        "message": f"Successfully generated {body.n} new videos for the to-do list.",
    }
