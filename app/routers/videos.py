"""Videos router – list, status update, queue addition, and YouTube sync."""

import uuid
from datetime import datetime
from typing import Optional

from app.timezone import now_ist, to_ist_iso

from dateutil.parser import isoparse

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, File, status
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


def _trigger_retention_analysis(
    channel_id: str,
    video_id: str,
    db: "AsyncIOMotorDatabase",
) -> None:
    """Fire a background task for video retention analysis."""
    import asyncio
    from app.main import r2_service, gemini_service  # type: ignore[import]
    from app.services.retention_analysis import run_retention_analysis

    if not r2_service or not gemini_service:
        logger.warning("Cannot run retention analysis — services not initialised")
        return

    asyncio.create_task(
        run_retention_analysis(channel_id, video_id, db, r2_service, gemini_service)
    )


# ------------------------------------------------------------------
# GET /  –  video list (with optional suggest_n)
# ------------------------------------------------------------------


from app.models.video import Video

@router.get("/")
async def list_videos(
    channel_id: str,
    status_filter: Optional[str] = None,
    verification_status: Optional[str] = None,
    suggest_n: Optional[int] = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return videos for *channel_id*.

    Query params
    ------------
    status : ``todo`` | ``ready`` | ``scheduled`` | ``published`` | ``all`` (default ``all``)
    verification_status : ``unverified`` | ``verified`` | ``missing`` — filter by verification state
    suggest_n : if provided, mark the top *n* to-do videos as suggested.
    """
    query: dict = {"channel_id": channel_id}
    if status_filter and status_filter != "all":
        query["status"] = status_filter
    if verification_status:
        if verification_status == "missing":
            query["verification_status"] = None
        else:
            query["verification_status"] = verification_status

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

    # Strip Mongo _id and serialize datetimes in GMT+5:30 (IST) for API response.
    for v in videos:
        v.pop("_id", None)
        for key in ("scheduled_at", "published_at", "created_at", "updated_at"):
            if v.get(key) is not None:
                v[key] = to_ist_iso(v[key])

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
    """Compare platform video IDs against our DB to get accurate sync numbers."""
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        return {"available": False, "reason": "Channel not found"}

    platform = channel.get("platform", "youtube")

    if platform == "instagram":
        return await _get_instagram_sync_status(channel_id, channel, db)

    if not channel.get("youtube_channel_id"):
        return {"available": False, "reason": "No YouTube channel linked"}

    youtube_service, _ = await _get_services(channel_id)
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


async def _get_instagram_sync_status(channel_id: str, channel: dict, db) -> dict:
    """Compute sync status for an Instagram channel."""
    ig_svc = await _get_instagram_service(channel_id)
    if ig_svc is None:
        return {"available": False, "reason": "No Instagram token for this channel"}

    ig_user_id = channel.get("instagram_user_id")
    if not ig_user_id:
        return {"available": False, "reason": "No instagram_user_id set"}

    try:
        reels = ig_svc.get_reels(ig_user_id)
        ig_media_ids = {r["id"] for r in reels}
    except Exception as exc:
        logger.warning("Could not fetch Instagram reels for sync status: %s", exc)
        return {"available": False, "reason": "Failed to reach Instagram API"}

    db_ig_ids: set[str] = set()
    async for doc in db.videos.find(
        {"channel_id": channel_id, "instagram_media_id": {"$ne": None}},
        {"instagram_media_id": 1},
    ):
        db_ig_ids.add(doc["instagram_media_id"])

    return {
        "available": True,
        "instagram_total": len(ig_media_ids),
        "in_database": len(db_ig_ids),
        "new_reels_to_import": len(ig_media_ids - db_ig_ids),
        "metadata_to_refresh": len(ig_media_ids & db_ig_ids),
    }


# ------------------------------------------------------------------
# PATCH /{video_id}/status  –  mark done / todo
# ------------------------------------------------------------------


_VALID_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "todo": {"published"},
    "ready": {"todo", "published"},
    "scheduled": {"todo", "published"},
    "published": set(),
}


@router.patch("/{video_id}/status")
async def update_video_status(
    channel_id: str,
    video_id: str,
    body: VideoStatusUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Change a video's lifecycle status.

    Valid transitions (other paths use dedicated endpoints like upload/schedule):
      todo -> published | ready -> todo, published | scheduled -> todo, published
    ``published`` is terminal.
    """
    from app.services.todo_engine import recompute_category

    video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video {video_id} not found",
        )

    old_status = video.get("status")
    new_status = body.status.value

    allowed = _VALID_STATUS_TRANSITIONS.get(old_status, set())
    if new_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status transition: '{old_status}' -> '{new_status}'. Allowed: {sorted(allowed) or 'none (terminal)'}",
        )

    update_fields: dict = {
        "status": new_status,
        "updated_at": now_ist(),
    }

    # Cleanup: leaving 'ready' — delete R2 file and remove from posting queue
    if old_status == "ready":
        r2_key = video.get("r2_object_key")
        if r2_key:
            try:
                r2 = _get_r2()
                r2.delete_video(r2_key)
            except Exception as exc:
                logger.warning(f"Failed to delete R2 object {r2_key} for video {video_id}: {exc}")
        await db.posting_queue.delete_one({"channel_id": channel_id, "video_id": video_id})
        update_fields["r2_object_key"] = None

    # Cleanup: leaving 'scheduled' — remove from schedule queue
    if old_status == "scheduled":
        await db.schedule_queue.delete_one({"channel_id": channel_id, "video_id": video_id})
        update_fields["scheduled_at"] = None

    # Moving to 'published'
    if new_status == "published":
        update_fields["published_at"] = now_ist()

    # Perform update
    await db.videos.update_one(
        {"_id": video["_id"]},
        {"$set": update_fields},
    )

    # Recompute category counts when entering or leaving 'published'
    if video.get("category") and (new_status == "published" or old_status == "published"):
        await recompute_category(channel_id, video["category"], db)

    logger.success("✅ Video '%s' status: %s → %s", video.get("title", video_id)[:50], old_status, new_status)

    return {"ok": True, "video_id": video_id, "status": new_status}


# ------------------------------------------------------------------
# PATCH /{video_id}/category  –  move video to another category
# ------------------------------------------------------------------


class VideoCategoryChange(BaseModel):
    """Body for changing a video's category."""

    old_category_id: str = Field(..., description="UUID of the current category")
    new_category_id: str = Field(..., description="UUID of the target category")


@router.patch("/{video_id}/category")
async def change_video_category(
    channel_id: str,
    video_id: str,
    body: VideoCategoryChange,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Move a video from one category to another.

    Updates the video document, the per-video analysis record in analysis_history,
    and recomputes metadata/video_count/video_ids for both categories.
    """
    video = await db.videos.find_one(
        {"channel_id": channel_id, "video_id": video_id}
    )
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video {video_id} not found",
        )

    old_cat = await db.categories.find_one(
        {"id": body.old_category_id, "channel_id": channel_id}
    )
    new_cat = await db.categories.find_one(
        {"id": body.new_category_id, "channel_id": channel_id}
    )
    if not old_cat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Old category not found",
        )
    if not new_cat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="New category not found",
        )

    if old_cat.get("status") == "archived":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot move video from an archived category",
        )
    if new_cat.get("status") == "archived":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot move video to an archived category",
        )

    old_name = old_cat["name"]
    new_name = new_cat["name"]
    if video.get("category") != old_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Video category is '{video.get('category')}', not '{old_name}'",
        )

    # Update video
    await db.videos.update_one(
        {"channel_id": channel_id, "video_id": video_id},
        {"$set": {"category": new_name, "updated_at": now_ist()}},
    )

    # Update all per-video analysis records (use update_many for safety)
    await db.analysis_history.update_many(
        {"channel_id": channel_id, "video_id": video_id},
        {"$set": {"category": new_name}},
    )

    # Recompute metadata + video_count + video_ids for both categories
    from app.services.todo_engine import recompute_category

    await recompute_category(channel_id, old_name, db)
    await recompute_category(channel_id, new_name, db)

    logger.success("✅ Moved video '%s' from '%s' → '%s'", video.get("title", video_id)[:50], old_name, new_name)

    return {
        "ok": True,
        "video_id": video_id,
        "old_category": old_name,
        "new_category": new_name,
    }


# ------------------------------------------------------------------
# POST /{video_id}/extract-params  –  extract content params via Gemini
# ------------------------------------------------------------------


@router.post("/{video_id}/extract-params")
async def extract_content_params(
    channel_id: str,
    video_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Use Gemini to extract content parameters from a video's metadata.

    Reads content parameter definitions from the ``content_params``
    collection and asks Gemini to identify values from the video's title,
    description, and tags.  Results are saved on the video document with
    ``verification_status = 'unverified'``.
    """
    import json

    _, gemini_service = await _get_services(channel_id)
    if gemini_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video {video_id} not found",
        )

    from app.database import get_content_schema_for_prompt
    schema_defs = await get_content_schema_for_prompt(db, channel_id)
    if not schema_defs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Channel has no content params defined — add them via POST /channels/{channel_id}/content-params",
        )

    channel_doc = await db.channels.find_one({"channel_id": channel_id})
    platform = (channel_doc or {}).get("platform", "youtube")
    persona = "YouTube content analyst" if platform == "youtube" else "Instagram Reels content analyst"

    prompt = f"""You are a {persona}. Extract content parameter values for this video.

## Content Parameter Schema
Each parameter below defines a dimension to classify the video by.
If the parameter has `values`, pick ONE of them. If `values` is empty, infer a concise free-form value.

```json
{json.dumps(schema_defs, indent=2)}
```

## Video Information
- **Title**: {video.get("title", "")}
- **Description**: {video.get("description", "")[:1000]}
- **Tags**: {json.dumps(video.get("tags", [])[:20])}

## Required Output
Return a single JSON object mapping each schema parameter name to its extracted value.

Example: {{"simulation_type": "battle", "challenge_mechanic": "1v1"}}
"""

    text = await gemini_service._generate(prompt)

    try:
        params = json.loads(text)
        if not isinstance(params, dict):
            raise ValueError("Expected a JSON object")
    except (json.JSONDecodeError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gemini returned unparseable response for param extraction",
        )

    await db.videos.update_one(
        {"_id": video["_id"]},
        {
            "$set": {
                "content_params": params,
                "verification_status": "unverified",
                "updated_at": now_ist(),
            }
        },
    )

    logger.success("🔍 Extracted content params for '%s'", video.get("title", video_id)[:50])

    return {
        "ok": True,
        "video_id": video_id,
        "content_params": params,
        "verification_status": "unverified",
    }


# ------------------------------------------------------------------
# POST /extract-params/all  –  bulk extract params for all videos missing them
# ------------------------------------------------------------------


@router.post("/extract-params/all")
async def extract_all_content_params(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Bulk-extract content parameters for every video that doesn't have them yet."""
    import json

    _, gemini_service = await _get_services(channel_id)
    if gemini_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    from app.database import get_content_schema_for_prompt
    schema_defs = await get_content_schema_for_prompt(db, channel_id)
    if not schema_defs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Channel has no content params defined",
        )

    videos = await db.videos.find(
        {"channel_id": channel_id, "verification_status": None}
    ).to_list(length=None)

    if not videos:
        return {"ok": True, "extracted": 0, "message": "All videos already have content_params"}

    channel_doc = await db.channels.find_one({"channel_id": channel_id})
    platform = (channel_doc or {}).get("platform", "youtube")
    persona = "YouTube content analyst" if platform == "youtube" else "Instagram Reels content analyst"

    extracted = 0

    for video in videos:
        prompt = f"""You are a {persona}. Extract content parameter values for this video.

## Content Parameter Schema
{json.dumps(schema_defs, indent=2)}

## Video
- **Title**: {video.get("title", "")}
- **Description**: {video.get("description", "")[:1000]}
- **Tags**: {json.dumps(video.get("tags", [])[:20])}

Return a single JSON object mapping each schema parameter name to its extracted value."""

        try:
            text = await gemini_service._generate(prompt)
            params = json.loads(text)
            if not isinstance(params, dict):
                continue

            await db.videos.update_one(
                {"_id": video["_id"]},
                {
                    "$set": {
                        "content_params": params,
                        "verification_status": "unverified",
                        "updated_at": now_ist(),
                    }
                },
            )
            extracted += 1
        except Exception as exc:
            logger.warning("Failed to extract params for video '%s': %s", video.get("title", video["video_id"]), exc)

    logger.success("🔍 Bulk extracted content params: %d/%d videos for channel '%s'", extracted, len(videos), channel_id)

    return {"ok": True, "extracted": extracted, "total": len(videos)}


# ------------------------------------------------------------------
# POST /{video_id}/verify-params  –  mark content params as verified
# ------------------------------------------------------------------


class VerifyRequest(BaseModel):
    """Optional overrides when verifying a video (category + content params)."""
    category: Optional[str] = Field(
        None, description="Override category before marking verified"
    )
    content_params: Optional[dict[str, str]] = Field(
        None, description="Override content params before marking verified"
    )


@router.post("/{video_id}/verify-params")
async def verify_video(
    channel_id: str,
    video_id: str,
    body: Optional[VerifyRequest] = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Mark a video as verified (both category and content_params).

    Optionally pass corrected ``category`` and/or ``content_params``
    in the body to override the AI-assigned values before marking verified.
    """
    video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video {video_id} not found",
        )

    if not video.get("content_params") and (not body or not body.content_params):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video has no content_params to verify — run extract-params first, or provide params in the body",
        )

    update: dict = {
        "verification_status": "verified",
        "updated_at": now_ist(),
    }
    if body and body.content_params:
        update["content_params"] = body.content_params
    if body and body.category:
        update["category"] = body.category

    await db.videos.update_one({"_id": video["_id"]}, {"$set": update})

    final_params = body.content_params if (body and body.content_params) else video.get("content_params")
    final_category = body.category if (body and body.category) else video.get("category")
    logger.success("✅ Verified video '%s' — category: %s", video.get("title", video_id)[:50], final_category)
    return {
        "ok": True,
        "video_id": video_id,
        "category": final_category,
        "content_params": final_params,
        "verification_status": "verified",
    }


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
        
    logger.success("📤 Uploaded video '%s' to R2 — queue position %d", video.get("title", video_id)[:50], next_pos)

    # Fire background retention analysis
    _trigger_retention_analysis(channel_id, video_id, db)

    return {"ok": True, "video": updated_video, "queue_position": next_pos}


# ------------------------------------------------------------------
# POST /create  –  create ad-hoc video, upload to R2, mark ready
# ------------------------------------------------------------------


@router.post("/create", status_code=status.HTTP_201_CREATED)
async def create_video(
    channel_id: str,
    file: UploadFile = File(...),
    title: str = Form(...),
    description: str = Form(""),
    tags: str = Form(""),
    category: Optional[str] = Form(None),
    content_params: Optional[str] = Form(None),
    scheduled_at: Optional[str] = Form(None),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Create an ad-hoc video, upload its file to R2, and add to posting queue.

    If ``category`` and ``content_params`` are provided the video is marked
    ``verified``.  Otherwise it is created as ``unverified`` with category
    ``Uncategorized`` — the next sync will run Gemini extraction on it.
    """
    import json as _json

    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    # Parse tags (comma-separated or JSON array)
    parsed_tags: list[str] = []
    if tags:
        tags_stripped = tags.strip()
        if tags_stripped.startswith("["):
            try:
                parsed_tags = _json.loads(tags_stripped)
            except _json.JSONDecodeError:
                parsed_tags = [t.strip() for t in tags_stripped.split(",") if t.strip()]
        else:
            parsed_tags = [t.strip() for t in tags_stripped.split(",") if t.strip()]

    # Parse content_params (JSON string)
    parsed_params: dict | None = None
    if content_params:
        try:
            parsed_params = _json.loads(content_params)
        except _json.JSONDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="content_params must be a valid JSON object",
            )

    is_verified = bool(category and parsed_params)
    platform = channel.get("platform", "youtube")

    # Handle direct scheduling if requested
    parsed_scheduled_at: datetime | None = None
    if scheduled_at:
        try:
            parsed_scheduled_at = isoparse(scheduled_at)
            # Ensure it's timezone-aware (assume IST if naive)
            from app.timezone import IST
            if parsed_scheduled_at.tzinfo is None:
                parsed_scheduled_at = parsed_scheduled_at.replace(tzinfo=IST)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid scheduled_at format: '{scheduled_at}'. Use ISO 8601 string."
            )

    vid_id = str(uuid.uuid4())
    r2 = _get_r2()
    r2_key = f"{channel_id}/{vid_id}.mp4"
    r2.upload_video(file.file, r2_key)

    now = now_ist()
    doc = {
        "channel_id": channel_id,
        "video_id": vid_id,
        "title": title,
        "description": description,
        "tags": parsed_tags,
        "category": category or "Uncategorized",
        "status": "scheduled" if (parsed_scheduled_at and platform == "instagram") else "ready",
        "suggested": False,
        "youtube_video_id": None,
        "instagram_media_id": None,
        "r2_object_key": r2_key,
        "metadata": {"views": None, "likes": None, "comments": None},
        "content_params": parsed_params,
        "verification_status": "verified" if is_verified else "unverified",
        "scheduled_at": parsed_scheduled_at if (parsed_scheduled_at and platform == "instagram") else None,
        "created_at": now,
        "updated_at": now,
    }
    await db.videos.insert_one(doc)
    doc.pop("_id", None)

    if doc["status"] == "scheduled":
        last = await db.schedule_queue.find_one(
            {"channel_id": channel_id},
            sort=[("position", -1)],
        )
        next_pos = (last["position"] + 1) if last else 1
        await db.schedule_queue.insert_one(
            {
                "channel_id": channel_id,
                "video_id": vid_id,
                "position": next_pos,
                "scheduled_at": doc["scheduled_at"],
                "added_at": now,
            }
        )
        queue_type = "scheduled"
    else:
        # Add to posting queue.
        last = await db.posting_queue.find_one(
            {"channel_id": channel_id},
            sort=[("position", -1)],
        )
        next_pos = (last["position"] + 1) if last else 1
        await db.posting_queue.insert_one(
            {
                "channel_id": channel_id,
                "video_id": vid_id,
                "position": next_pos,
                "added_at": now,
            }
        )
        queue_type = "ready"

    logger.success(
        "Created ad-hoc video '%s' — status=%s, verified=%s, %s queue position %d",
        title[:50], doc["status"], is_verified, queue_type, next_pos,
    )

    # Fire background retention analysis if the video is ready (has R2 file)
    if doc["status"] == "ready":
        _trigger_retention_analysis(channel_id, vid_id, db)

    return {"ok": True, "video": doc, "queue_position": next_pos}


# ------------------------------------------------------------------
# POST /{video_id}/schedule  –  schedule video(s) on platform
# ------------------------------------------------------------------


class ScheduleRequest(BaseModel):
    """Optional manual scheduling parameters."""
    scheduled_at: Optional[datetime] = Field(
        None, description="Manually specify a publish time (ISO string). Only valid when scheduling a single video_id."
    )


@router.post("/{video_id}/schedule")
async def schedule_video(
    channel_id: str,
    video_id: str,
    body: Optional[ScheduleRequest] = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Schedule video(s) from the **ready queue** on the platform.

    - Pass a specific ``video_id`` to schedule one video.
    - Pass ``"all"`` as ``video_id`` to schedule every video in the ready queue.
    - Optionally pass ``scheduled_at`` in the body to manually pick the time (only for single videos).

    **YouTube**: uploads to YouTube as private with ``publishAt`` immediately.
    **Instagram**: queues for the background auto-publisher which uploads
    and publishes the reel when ``scheduled_at`` arrives.
    """
    from app.config import get_settings
    from app.services.scheduler import compute_schedule_slots
    from app.services.schedule_operation import (
        schedule_single_video,
        schedule_single_video_instagram,
    )

    settings = get_settings()
    r2_service = _get_r2()

    channel_doc = await db.channels.find_one({"channel_id": channel_id})
    if not channel_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    platform = channel_doc.get("platform", "youtube")

    if platform == "youtube":
        youtube_service, _ = await _get_services(channel_id)
        if youtube_service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"No YouTube token for channel '{channel_id}'. Store tokens via POST /channels/{channel_id}/youtube-token",
            )
    elif platform == "instagram":
        ig_service = await _get_instagram_service(channel_id)
        if ig_service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"No Instagram token for channel '{channel_id}'. Store tokens via POST /channels/{channel_id}/instagram-token",
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported platform '{platform}'",
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

    # ---- Compute or use manual publish slots ----
    is_all = video_id.lower() == "all"
    manual_dt = body.scheduled_at if (body and not is_all) else None

    if manual_dt:
        slots = [manual_dt]
    else:
        analysis = await db.analysis.find_one({"channel_id": channel_id})
        if not analysis or not analysis.get("best_posting_times"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No analysis with best_posting_times found — run analysis first",
            )

        existing_scheduled = await db.schedule_queue.find(
            {"channel_id": channel_id}
        ).to_list(length=None)
        occupied_datetimes = [e.get("scheduled_at") for e in existing_scheduled]

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

    # ---- Schedule each video ----
    results = []
    scheduled = 0
    failed = 0

    for video_doc, slot_dt in zip(videos_to_schedule, slots):
        if platform == "youtube":
            result = await schedule_single_video(
                db=db,
                r2_service=r2_service,
                youtube_service=youtube_service,
                channel_id=channel_id,
                video_doc=video_doc,
                scheduled_at=slot_dt,
            )
        else:
            result = await schedule_single_video_instagram(
                db=db,
                channel_id=channel_id,
                video_doc=video_doc,
                scheduled_at=slot_dt,
            )
        results.append(result)
        if result["status"] == "scheduled":
            scheduled += 1
        else:
            failed += 1

    platform_label = "YouTube" if platform == "youtube" else "Instagram"
    logger.success("Scheduled %d video(s) for %s channel '%s' (%d failed)", scheduled, platform_label, channel_id, failed)

    return {
        "ok": True,
        "scheduled": scheduled,
        "failed": failed,
        "videos": results,
    }


# ------------------------------------------------------------------
# POST /sync  –  sync videos from YouTube + categorise via Gemini
# ------------------------------------------------------------------


async def _get_services(channel_id: str):
    """Lazy import to avoid circular dependency."""
    from app.main import youtube_service_manager, gemini_service  # type: ignore[import]

    youtube_service = await youtube_service_manager.get_service(channel_id) if youtube_service_manager else None
    return youtube_service, gemini_service


async def _get_instagram_service(channel_id: str):
    """Lazy import for Instagram service manager."""
    from app.main import instagram_service_manager  # type: ignore[import]

    if instagram_service_manager is None:
        return None
    return await instagram_service_manager.get_service(channel_id)


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
                part="snippet,statistics,contentDetails,status",
                id=",".join(batch_ids),
            )
            .execute()
        )
        for item in resp.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})
            yt_status = item.get("status", {})

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
                    "privacy_status": yt_status.get("privacyStatus", ""),
                    "publish_at": yt_status.get("publishAt"),
                    "views": views,
                    "likes": likes,
                    "comments": comments,
                    "duration_seconds": duration_seconds,
                    **rates,
                }
            )

    return videos


async def _extract_params_and_categorize_batch(
    gemini_service, content_schema, existing_categories, batch,
    category_instructions="", platform="youtube",
):
    """Extract content params AND derive category for a batch of videos in one Gemini call.

    Returns a list of dicts with the platform-specific ID key
    (``youtube_video_id`` or ``instagram_media_id``).
    """
    import json

    id_key = "youtube_video_id" if platform == "youtube" else "instagram_media_id"
    persona = "YouTube channel analyst" if platform == "youtube" else "Instagram Reels analyst"

    video_summaries = [
        {
            id_key: v.get(id_key, v.get("youtube_video_id", v.get("instagram_media_id", ""))),
            "title": v["title"],
            "description": v["description"][:500],
            "tags": v["tags"][:15],
        }
        for v in batch
    ]

    schema_section = ""
    if content_schema:
        schema_section = (
            f"\n\n## Content Parameter Schema\n"
            f"Extract values for each of these dimensions. If `values` is non-empty, pick one. "
            f"If empty, infer a concise free-form value.\n"
            f"```json\n{json.dumps(content_schema, indent=2)}\n```"
        )

    cats_section = ""
    if existing_categories:
        cats_section = (
            f"\n\n## Existing Categories\n"
            f"Derive the category from the extracted content_params. "
            f"Reuse these when a video fits: {json.dumps(existing_categories)}\n"
            f"Only create a new category if NONE of the above fit."
        )

    prompt = f"""You are a {persona}. For each video below:
1. Extract content parameter values based on the schema.
2. Derive a category from those extracted parameters (NOT from title/description/tags directly).

{schema_section}
{cats_section}

## Videos
```json
{json.dumps(video_summaries, indent=2)}
```

{f"## Additional Instructions" + chr(10) + category_instructions if category_instructions else ""}

Return a JSON array:
[{{"{id_key}": "...", "content_params": {{"param1": "value1", "param2": "value2"}}, "category": "..."}}]

Reuse existing categories. Only create new ones if truly needed."""

    response_text = await gemini_service._generate(prompt)

    try:
        return json.loads(response_text)
    except (json.JSONDecodeError, TypeError):
        return []


async def _process_unverified_videos(
    channel_id: str,
    gemini_service,
    body,
    db,
    *,
    existing_cats: list[str] | None = None,
    content_schema: str | None = None,
) -> int:
    """Find unverified videos in DB and run Gemini extraction on them.

    Returns the number of videos processed.
    """
    unverified_docs = await db.videos.find(
        {"channel_id": channel_id, "verification_status": "unverified", "content_params": None},
    ).to_list(length=None)

    if not unverified_docs:
        return 0

    logger.info(
        "Found %d unverified video(s) — running Gemini extraction...",
        len(unverified_docs),
        extra={"color": "MAGENTA"},
    )

    if content_schema is None:
        from app.database import get_content_schema_for_prompt
        content_schema = await get_content_schema_for_prompt(db, channel_id)

    if existing_cats is None:
        existing_cats = [
            c["name"]
            async for c in db.categories.find({"channel_id": channel_id}, {"name": 1})
        ]

    BATCH_SIZE = 5
    uv_batch_items = [
        {
            "youtube_video_id": doc["video_id"],
            "title": doc.get("title", ""),
            "description": doc.get("description", "")[:500],
            "tags": doc.get("tags", [])[:15],
        }
        for doc in unverified_docs
    ]

    updated = 0
    for i in range(0, len(uv_batch_items), BATCH_SIZE):
        batch = uv_batch_items[i : i + BATCH_SIZE]
        results = await _extract_params_and_categorize_batch(
            gemini_service, content_schema, existing_cats, batch,
            category_instructions=body.new_category_description if body else "",
        )
        for r in results:
            vid_id = r.get("youtube_video_id", "")
            cat = r.get("category", "Uncategorized")
            params = r.get("content_params", {})

            if cat not in existing_cats:
                existing_cats.append(cat)
                await db.categories.insert_one({
                    "id": str(uuid.uuid4()),
                    "channel_id": channel_id,
                    "name": cat,
                    "description": "",
                    "raw_description": "",
                    "score": 0,
                    "status": "active",
                    "video_count": 0,
                    "metadata": {"total_videos": 0},
                    "created_at": now_ist(),
                    "updated_at": now_ist(),
                })
                logger.success("Created new category: '%s'", cat)

            await db.videos.update_one(
                {"channel_id": channel_id, "video_id": vid_id},
                {"$set": {
                    "category": cat,
                    "content_params": params if params else None,
                    "verification_status": "unverified",
                    "updated_at": now_ist(),
                }},
            )
            updated += 1

    logger.success("Extracted params for %d unverified video(s).", updated)
    return updated


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
    """Sync videos from the appropriate platform into the DB.

    For YouTube: fetches all videos, categorises via Gemini, inserts.
    For Instagram: fetches all reels, fetches insights, categorises via Gemini, inserts.
    """
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    platform = channel.get("platform", "youtube")

    if platform == "instagram":
        return await _sync_instagram_reels(channel_id, channel, body, db)

    # --- YouTube sync ---
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

    youtube_channel_id = channel.get("youtube_channel_id")
    if not youtube_channel_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Channel has no youtube_channel_id",
        )
    channel_description = channel.get("description", "")

    # Fetch all YouTube videos (Data API: snippet + statistics + contentDetails).
    all_yt_videos = _fetch_all_youtube_videos(youtube_service, youtube_channel_id)

    # Enrich with Analytics API data (avg_percentage_viewed, avg_view_duration, etc.)
    all_yt_ids = [v["youtube_video_id"] for v in all_yt_videos]
    analytics_data = youtube_service.get_video_analytics(all_yt_ids)
    for v in all_yt_videos:
        extra = analytics_data.get(v["youtube_video_id"], {})
        v.update(extra)

    # Fetch subscribers gained per video from YouTube Analytics API.
    subs_gained = youtube_service.get_subscribers_gained(all_yt_ids)
    for v in all_yt_videos:
        v["subscribers_gained"] = subs_gained.get(v["youtube_video_id"], 0)

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
                    "metadata.subscribers_gained": fresh.get("subscribers_gained", 0),
                    "updated_at": now_ist(),
                }
            },
        )
        metadata_updated += 1

    if metadata_updated:
        logger.success(f"Refreshed metadata for {metadata_updated} existing video(s).")
        from app.services.todo_engine import recompute_category
        refreshed_cats: set[str] = set()
        async for doc in db.videos.find(
            {"channel_id": channel_id, "youtube_video_id": {"$in": list(existing_yt_ids)}, "status": "published"},
            {"category": 1},
        ):
            if doc.get("category"):
                refreshed_cats.add(doc["category"])
        for cat_name in refreshed_cats:
            await recompute_category(channel_id, cat_name, db)
        logger.success(f"Recomputed metadata for {len(refreshed_cats)} category(ies).")

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
        from app.services.todo_engine import recompute_category
        reconciled_cats = {vid["category"] for vid in scheduled_videos if vid.get("category")}
        for cat_name in reconciled_cats:
            await recompute_category(channel_id, cat_name, db)
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
        # Still process any unverified videos in the DB.
        uv_result = await _process_unverified_videos(
            channel_id, gemini_service, body, db,
        )
        return {
            "ok": True,
            "synced": 0,
            "reconciled": reconciled,
            "metadata_refreshed": metadata_updated,
            "unverified_extracted": uv_result,
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

    from app.database import get_content_schema_for_prompt
    content_schema = await get_content_schema_for_prompt(db, channel_id)

    # Extract content params AND derive category in batches of 5.
    BATCH_SIZE = 5
    categorizations = {}

    for i in range(0, len(new_videos), BATCH_SIZE):
        batch = new_videos[i : i + BATCH_SIZE]
        
        logger.info(
            "Asking Gemini to extract params & categorize batch (%d/%d)...",
            (i // BATCH_SIZE) + 1,
            (len(new_videos) + BATCH_SIZE - 1) // BATCH_SIZE,
            extra={"color": "MAGENTA"},
        )
        
        results = await _extract_params_and_categorize_batch(
            gemini_service, content_schema, existing_cats, batch,
            category_instructions=body.new_category_description if body else "",
        )

        for r in results:
            yt_id = r.get("youtube_video_id", "")
            cat = r.get("category", "Uncategorized")
            params = r.get("content_params", {})
            categorizations[yt_id] = {
                "category": cat,
                "content_params": params,
            }

            if cat not in existing_cats:
                existing_cats.append(cat)
                now = now_ist()
                await db.categories.insert_one(
                    {
                        "id": str(uuid.uuid4()),
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
    # Scheduled (private + future publishAt) videos go to schedule_queue.
    # ------------------------------------------------------------------
    docs = []
    scheduled_docs = []

    for v in new_videos:
        yt_id = v["youtube_video_id"]
        cat_info = categorizations.get(yt_id, {"category": "Uncategorized", "content_params": {}})
        now = now_ist()

        yt_published_at = now
        if v.get("published_at"):
            try:
                from app.timezone import IST
                yt_published_at = isoparse(v["published_at"]).astimezone(IST)
            except (ValueError, TypeError):
                pass

        extracted_params = cat_info.get("content_params", {})

        is_scheduled = False
        scheduled_at_dt = None
        if v.get("publish_at"):
            try:
                from app.timezone import IST
                scheduled_at_dt = isoparse(v["publish_at"]).astimezone(IST)
                if scheduled_at_dt > now:
                    is_scheduled = True
            except (ValueError, TypeError):
                pass

        vid_id = str(uuid.uuid4())

        doc = {
            "channel_id": channel_id,
            "video_id": vid_id,
            "title": v["title"],
            "description": v["description"],
            "tags": v["tags"],
            "category": cat_info["category"],
            "status": "scheduled" if is_scheduled else "published",
            "suggested": False,
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
                "subscribers_gained": v.get("subscribers_gained", 0),
            },
            "content_params": extracted_params if extracted_params else None,
            "verification_status": "unverified" if extracted_params else None,
            "scheduled_at": scheduled_at_dt if is_scheduled else None,
            "published_at": None if is_scheduled else yt_published_at,
            "created_at": yt_published_at,
            "updated_at": now,
        }
        docs.append(doc)

        if is_scheduled:
            scheduled_docs.append(doc)

    if docs:
        await db.videos.insert_many(docs)
        logger.success(f"Inserted {len(docs)} new synchronized videos into database")

        # Add scheduled videos to the schedule_queue.
        if scheduled_docs:
            last = await db.schedule_queue.find_one(
                {"channel_id": channel_id},
                sort=[("position", -1)],
            )
            next_pos = (last["position"] + 1) if last else 1

            queue_entries = []
            for sd in scheduled_docs:
                queue_entries.append({
                    "channel_id": channel_id,
                    "video_id": sd["video_id"],
                    "position": next_pos,
                    "scheduled_at": sd["scheduled_at"],
                    "added_at": now_ist(),
                })
                next_pos += 1
            await db.schedule_queue.insert_many(queue_entries)
            logger.success(
                f"Added {len(scheduled_docs)} scheduled video(s) to schedule_queue"
            )

        from app.services.todo_engine import recompute_category
        affected_cats = {d["category"] for d in docs if d["status"] == "published"}
        for cat_name in affected_cats:
            await recompute_category(channel_id, cat_name, db)

    # Process unverified videos already in the DB (e.g. ad-hoc uploads).
    unverified_updated = await _process_unverified_videos(
        channel_id, gemini_service, body, db,
        existing_cats=existing_cats,
        content_schema=content_schema,
    )

    # Build per-video summary.
    video_summary = [
        {"title": d["title"], "category": d["category"], "status": d["status"]}
        for d in docs
    ]

    published_count = sum(1 for d in docs if d["status"] == "published")
    scheduled_count = len(scheduled_docs)

    logger.success(
        f"✅ YouTube Sync Complete! Synced {len(docs)} new videos "
        f"({published_count} published, {scheduled_count} scheduled).",
        extra={"color": "BRIGHT_GREEN"}
    )

    return {
        "ok": True,
        "synced": len(docs),
        "synced_published": published_count,
        "synced_scheduled": scheduled_count,
        "reconciled": reconciled,
        "metadata_refreshed": metadata_updated,
        "unverified_extracted": unverified_updated,
        "categories_created": [
            c for c in existing_cats
        ],
        "videos": video_summary,
    }


# ------------------------------------------------------------------
# Instagram reel sync
# ------------------------------------------------------------------


async def _sync_instagram_reels(
    channel_id: str,
    channel: dict,
    body: Optional[SyncRequest],
    db: AsyncIOMotorDatabase,
) -> dict:
    """Import Instagram reels for *channel_id*, categorise via Gemini, and insert."""
    ig_svc = await _get_instagram_service(channel_id)
    if ig_svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No Instagram token for channel '{channel_id}'. Store tokens via POST /channels/{channel_id}/instagram-token",
        )

    _, gemini_service = await _get_services(channel_id)
    if gemini_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    ig_user_id = channel.get("instagram_user_id")
    if not ig_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Channel has no instagram_user_id",
        )

    reels = ig_svc.get_reels(ig_user_id)
    if not reels:
        return {"ok": True, "synced": 0, "message": "No reels found on Instagram"}

    media_ids = [r["id"] for r in reels]
    insights = ig_svc.get_reel_insights(media_ids)

    existing_ig_ids: set[str] = set()
    async for doc in db.videos.find(
        {"channel_id": channel_id, "instagram_media_id": {"$ne": None}},
        {"instagram_media_id": 1, "_id": 1},
    ):
        existing_ig_ids.add(doc["instagram_media_id"])

    # Refresh metrics for existing reels
    metadata_updated = 0
    for reel in reels:
        mid = reel["id"]
        if mid not in existing_ig_ids:
            continue
        reel_insights = insights.get(mid, {})
        views = reel_insights.get("views", reel.get("like_count", 0))
        like_count = reel.get("like_count", 0)
        comments_count = reel.get("comments_count", 0)
        shares = reel_insights.get("shares", 0)
        saves = reel_insights.get("saved", 0)
        reach_val = reel_insights.get("reach", 0)

        rates = _compute_rates(views, like_count, comments_count)
        ig_engagement = None
        if reach_val and reach_val > 0:
            ig_engagement = round((like_count + comments_count + shares + saves) / reach_val * 100, 4)

        await db.videos.update_one(
            {"channel_id": channel_id, "instagram_media_id": mid},
            {
                "$set": {
                    "metadata.views": views,
                    "metadata.likes": like_count,
                    "metadata.comments": comments_count,
                    "metadata.shares": shares,
                    "metadata.saves": saves,
                    "metadata.reach": reach_val,
                    "metadata.engagement_rate": ig_engagement or rates.get("engagement_rate"),
                    "metadata.like_rate": rates.get("like_rate"),
                    "metadata.comment_rate": rates.get("comment_rate"),
                    "updated_at": now_ist(),
                }
            },
        )
        metadata_updated += 1

    if metadata_updated:
        logger.success("Refreshed metrics for %d existing reel(s).", metadata_updated)
        from app.services.todo_engine import recompute_category
        refreshed_cats: set[str] = set()
        async for doc in db.videos.find(
            {"channel_id": channel_id, "instagram_media_id": {"$in": list(existing_ig_ids)}, "status": "published"},
            {"category": 1},
        ):
            if doc.get("category"):
                refreshed_cats.add(doc["category"])
        for cat_name in refreshed_cats:
            await recompute_category(channel_id, cat_name, db)
        logger.success("Recomputed metadata for %d category(ies).", len(refreshed_cats))

    new_reels = [r for r in reels if r["id"] not in existing_ig_ids]

    logger.info(
        "Found %d total reels on Instagram. %d existing (metrics refreshed), %d new to process.",
        len(reels), len(existing_ig_ids), len(new_reels),
        extra={"color": "BLUE"},
    )

    if not new_reels:
        uv_result = await _process_unverified_videos(
            channel_id, gemini_service, body, db,
        )
        return {
            "ok": True,
            "synced": 0,
            "metadata_refreshed": metadata_updated,
            "unverified_extracted": uv_result,
            "message": f"All {len(reels)} reels already in DB — metrics refreshed",
            "videos": [],
        }

    # Categorise new reels via Gemini
    existing_cats = [
        c["name"]
        async for c in db.categories.find({"channel_id": channel_id}, {"name": 1})
    ]

    from app.database import get_content_schema_for_prompt
    content_schema = await get_content_schema_for_prompt(db, channel_id)

    BATCH_SIZE = 5
    categorizations: dict[str, dict] = {}

    ig_videos_for_gemini = []
    for reel in new_reels:
        caption = reel.get("caption", "") or ""
        lines = caption.strip().split("\n")
        title = lines[0][:100] if lines else "Untitled"
        tags = [w.strip("#") for w in caption.split() if w.startswith("#")]
        ig_videos_for_gemini.append({
            "instagram_media_id": reel["id"],
            "title": title,
            "description": caption,
            "tags": tags[:15],
        })

    for i in range(0, len(ig_videos_for_gemini), BATCH_SIZE):
        batch = ig_videos_for_gemini[i : i + BATCH_SIZE]
        logger.info(
            "Asking Gemini to extract params & categorize reel batch (%d/%d)...",
            (i // BATCH_SIZE) + 1,
            (len(ig_videos_for_gemini) + BATCH_SIZE - 1) // BATCH_SIZE,
            extra={"color": "MAGENTA"},
        )
        results = await _extract_params_and_categorize_batch(
            gemini_service, content_schema, existing_cats, batch,
            category_instructions=body.new_category_description if body else "",
            platform="instagram",
        )
        for r in results:
            mid = r.get("instagram_media_id", "")
            cat = r.get("category", "Uncategorized")
            params = r.get("content_params", {})
            categorizations[mid] = {"category": cat, "content_params": params}
            if cat not in existing_cats:
                existing_cats.append(cat)
                now = now_ist()
                await db.categories.insert_one({
                    "id": str(uuid.uuid4()),
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
                })
                logger.success("Created new category: '%s'", cat)

    docs = []
    for reel in new_reels:
        mid = reel["id"]
        cat_info = categorizations.get(mid, {"category": "Uncategorized", "content_params": {}})
        reel_insights = insights.get(mid, {})
        caption = reel.get("caption", "") or ""
        lines = caption.strip().split("\n")
        title = lines[0][:100] if lines else "Untitled"
        tags = [w.strip("#") for w in caption.split() if w.startswith("#")]

        views = reel_insights.get("views", 0)
        like_count = reel.get("like_count", 0)
        comments_count = reel.get("comments_count", 0)
        shares = reel_insights.get("shares", 0)
        saves = reel_insights.get("saved", 0)
        reach_val = reel_insights.get("reach", 0)

        rates = _compute_rates(views, like_count, comments_count)
        ig_engagement = None
        if reach_val and reach_val > 0:
            ig_engagement = round((like_count + comments_count + shares + saves) / reach_val * 100, 4)

        published_at = now_ist()
        ts = reel.get("timestamp")
        if ts:
            try:
                published_at = isoparse(ts)
            except (ValueError, TypeError):
                pass

        extracted_params = cat_info.get("content_params", {})

        doc = {
            "channel_id": channel_id,
            "video_id": str(uuid.uuid4()),
            "title": title,
            "description": caption,
            "tags": tags,
            "category": cat_info["category"],
            "status": "published",
            "suggested": False,
            "youtube_video_id": None,
            "instagram_media_id": mid,
            "r2_object_key": None,
            "metadata": {
                "views": views,
                "likes": like_count,
                "comments": comments_count,
                "shares": shares,
                "saves": saves,
                "reach": reach_val,
                "engagement_rate": ig_engagement or rates.get("engagement_rate"),
                "like_rate": rates.get("like_rate"),
                "comment_rate": rates.get("comment_rate"),
            },
            "content_params": extracted_params if extracted_params else None,
            "verification_status": "unverified" if extracted_params else None,
            "scheduled_at": None,
            "published_at": published_at,
            "created_at": published_at,
            "updated_at": now_ist(),
        }
        docs.append(doc)

    if docs:
        await db.videos.insert_many(docs)
        logger.success("Inserted %d new Instagram reels into database", len(docs))

        from app.services.todo_engine import recompute_category
        affected_cats = {d["category"] for d in docs}
        for cat_name in affected_cats:
            await recompute_category(channel_id, cat_name, db)

    # Process unverified videos already in the DB (e.g. ad-hoc uploads).
    unverified_updated = await _process_unverified_videos(
        channel_id, gemini_service, body, db,
        existing_cats=existing_cats,
        content_schema=content_schema,
    )

    video_summary = [
        {"title": d["title"], "category": d["category"], "status": d["status"]}
        for d in docs
    ]

    logger.success(
        "✅ Instagram Sync Complete! Synced %d new reels.",
        len(docs),
        extra={"color": "BRIGHT_GREEN"},
    )

    return {
        "ok": True,
        "synced": len(docs),
        "metadata_refreshed": metadata_updated,
        "unverified_extracted": unverified_updated,
        "categories_created": existing_cats,
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

    _, gemini_service = await _get_services(channel_id)

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

    logger.success("📝 Generated %d new to-do videos for channel '%s'", body.n, channel_id)

    return {
        "ok": True,
        "message": f"Successfully generated {body.n} new videos for the to-do list.",
    }


# ------------------------------------------------------------------
# DELETE /{video_id}  –  remove a video and its assets
# ------------------------------------------------------------------


@router.delete("/{video_id}")
async def delete_video(
    channel_id: str,
    video_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Delete a video document and clean up all associated assets and queue entries."""
    video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video {video_id} not found",
        )

    # 1. Clean up R2 storage
    r2_key = video.get("r2_object_key")
    if r2_key:
        try:
            r2 = _get_r2()
            r2.delete_video(r2_key)
        except Exception as exc:
            logger.warning(f"Failed to delete R2 object {r2_key} for video {video_id}: {exc}")

    was_published = video.get("status") == "published"
    category_name = video.get("category")

    # 2. Remove from all possible queues
    await db.posting_queue.delete_one({"channel_id": channel_id, "video_id": video_id})
    await db.schedule_queue.delete_one({"channel_id": channel_id, "video_id": video_id})

    # 3. Delete the video document, analysis history, and retention analysis
    await db.videos.delete_one({"_id": video["_id"]})
    await db.analysis_history.delete_many({"channel_id": channel_id, "video_id": video_id})
    await db.retention_analysis.delete_one({"channel_id": channel_id, "video_id": video_id})

    # 4. Recompute category after deletion
    if was_published and category_name:
        from app.services.todo_engine import recompute_category
        await recompute_category(channel_id, category_name, db)

    logger.success("🗑️ Deleted video '%s' from channel '%s'", video.get("title", video_id)[:50], channel_id)

    return {"ok": True, "video_id": video_id, "deleted": True}
