"""Videos router – list, status update, queue addition, and YouTube sync."""

import uuid
from datetime import datetime
from typing import Optional

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

@router.get("/", response_model=list[Video])
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
            {"$set": {"suggested": False, "updated_at": datetime.utcnow()}},
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
                {"$set": {"suggested": True, "updated_at": datetime.utcnow()}},
            )

    videos = await db.videos.find(query).to_list(length=None)

    # Strip Mongo _id for JSON serialisation.
    for v in videos:
        v.pop("_id", None)

    return videos


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
    result = await db.videos.update_one(
        {"channel_id": channel_id, "video_id": video_id},
        {
            "$set": {
                "status": body.status.value,
                "updated_at": datetime.utcnow(),
            }
        },
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
# POST /queue  –  add video to posting queue (+ videos collection)
# ------------------------------------------------------------------


@router.post("/{video_id}/queue", status_code=status.HTTP_201_CREATED)
async def add_to_queue(
    channel_id: str,
    video_id: str,
    file: UploadFile = File(...),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Upload a video file for an existing ``todo`` video and add it to the posting queue.

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

    now = datetime.utcnow()

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
# POST /{video_id}/schedule  –  move video from posting_queue → schedule_queue
# ------------------------------------------------------------------


@router.post("/{video_id}/schedule")
async def schedule_video(
    channel_id: str,
    video_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Move **ready** video(s) into the schedule queue with publish times.

    - Pass a specific ``video_id`` to schedule one video.
    - Pass ``"all"`` as ``video_id`` to schedule every video in the posting queue.

    Publish times are computed from the channel's ``best_posting_times``
    analysis, skipping any slots already occupied by previously scheduled
    videos.
    """
    from app.config import get_settings
    from app.services.scheduler import compute_schedule_slots

    settings = get_settings()

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
                detail="No videos in the posting queue to schedule",
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

    # ---- Gather already-occupied slots from existing schedule queue ----
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

    # ---- Determine starting position ----
    last = await db.schedule_queue.find_one(
        {"channel_id": channel_id},
        sort=[("position", -1)],
    )
    next_pos = (last["position"] + 1) if last else 1

    now = datetime.utcnow()
    results = []

    for video_doc, slot_dt in zip(videos_to_schedule, slots):
        vid = video_doc["video_id"]

        await db.posting_queue.delete_one(
            {"channel_id": channel_id, "video_id": vid}
        )

        await db.schedule_queue.insert_one(
            {
                "channel_id": channel_id,
                "video_id": vid,
                "position": next_pos,
                "scheduled_at": slot_dt,
                "added_at": now,
            }
        )

        await db.videos.update_one(
            {"channel_id": channel_id, "video_id": vid},
            {"$set": {"status": "scheduled", "updated_at": now}},
        )

        results.append({
            "video_id": vid,
            "title": video_doc.get("title", ""),
            "scheduled_at": slot_dt.isoformat(),
            "schedule_position": next_pos,
        })
        next_pos += 1

    return {
        "ok": True,
        "scheduled_count": len(results),
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

    # Find already-imported youtube_video_ids.
    existing_yt_ids = set()
    async for doc in db.videos.find(
        {"channel_id": channel_id}, {"youtube_video_id": 1}
    ):
        if doc.get("youtube_video_id"):
            existing_yt_ids.add(doc["youtube_video_id"])

    new_videos = [
        v for v in all_yt_videos if v["youtube_video_id"] not in existing_yt_ids
    ]
    
    logger.info(
        "Found %d total videos on channel. Skipped %d already imported, %d new to process.",
        len(all_yt_videos),
        len(existing_yt_ids),
        len(new_videos),
        extra={"color": "BLUE"},
    )

    if not new_videos:
        return {
            "ok": True,
            "synced": 0,
            "message": f"All {len(all_yt_videos)} videos already in DB",
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
                now = datetime.utcnow()
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

    # Insert videos.
    docs = []
    for v in new_videos:
        yt_id = v["youtube_video_id"]
        cat_info = categorizations.get(yt_id, {"category": "Uncategorized", "topic": ""})
        now = datetime.utcnow()

        # Use YouTube publish date as created_at so the 3-day filter works.
        published_at = now
        if v.get("published_at"):
            try:
                published_at = isoparse(v["published_at"]).replace(tzinfo=None)
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
                "created_at": published_at,
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
        "categories_created": [
            c for c in existing_cats
        ],
        "videos": video_summary,
    }

