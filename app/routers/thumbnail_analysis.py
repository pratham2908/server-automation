"""Thumbnail analysis router -- ephemeral image quality and CTR scoring."""

import asyncio
import tempfile
import uuid
from datetime import timedelta
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger
from app.timezone import now_ist

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/thumbnail-analysis",
    tags=["thumbnail-analysis"],
    dependencies=[Depends(verify_api_key)],
)

_TTL_HOURS = 24


def _get_image_suffix(filename: str | None) -> str:
    """Derive a file suffix from the upload filename, defaulting to .jpg."""
    if filename:
        for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            if filename.lower().endswith(ext):
                return ext
    return ".jpg"


# ------------------------------------------------------------------
# POST / -- upload a thumbnail image for analysis
# ------------------------------------------------------------------


@router.post("/", status_code=status.HTTP_202_ACCEPTED)
async def create_thumbnail_analysis(
    channel_id: str,
    file: UploadFile = File(...),
    title: str = Form("Untitled"),
    label: Optional[str] = Form(None),
    previous_analysis_id: Optional[str] = Form(None),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Upload a thumbnail image for ephemeral quality/CTR analysis.

    The image is analyzed in the background. Poll ``GET /{analysis_id}``
    for results. The analysis auto-expires after 24 hours.
    """
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found")

    if previous_analysis_id:
        prev = await db.thumbnail_analysis.find_one({"analysis_id": previous_analysis_id})
        if not prev:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Previous analysis '{previous_analysis_id}' not found (may have expired)",
            )

    platform = channel.get("platform", "youtube")
    analysis_id = str(uuid.uuid4())
    now = now_ist()

    suffix = _get_image_suffix(file.filename)
    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=suffix, prefix=f"thumb_{analysis_id}_",
    )
    try:
        contents = await file.read()
        tmp.write(contents)
        tmp.flush()
        tmp_path = tmp.name
    finally:
        tmp.close()

    doc: dict[str, Any] = {
        "analysis_id": analysis_id,
        "channel_id": channel_id,
        "title": title,
        "label": label,
        "previous_analysis_id": previous_analysis_id,
        "video_id": None,
        "platform": platform,
        "status": "analyzing",
        "analysis": None,
        "error_message": None,
        "created_at": now,
        "analyzed_at": None,
        "expires_at": now + timedelta(hours=_TTL_HOURS),
    }
    await db.thumbnail_analysis.insert_one(doc)

    import app.main as main_mod
    from app.services.thumbnail_analysis import run_thumbnail_analysis

    if not main_mod.gemini_service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    asyncio.create_task(
        run_thumbnail_analysis(
            analysis_id, tmp_path, title, platform, db, main_mod.gemini_service,
        )
    )

    return {
        "ok": True,
        "analysis_id": analysis_id,
        "message": "Thumbnail analysis started — poll GET /{analysis_id} for results",
        "expires_at": doc["expires_at"].isoformat(),
    }


# ------------------------------------------------------------------
# POST /video/{video_id} -- analyze thumbnail for an existing video
# ------------------------------------------------------------------


@router.post("/video/{video_id}", status_code=status.HTTP_202_ACCEPTED)
async def create_video_thumbnail_analysis(
    channel_id: str,
    video_id: str,
    label: Optional[str] = Query(None),
    previous_analysis_id: Optional[str] = Query(None),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Fetch and analyze the thumbnail of an existing video.

    For YouTube videos, the high-res thumbnail is fetched from
    ``img.youtube.com``. For Instagram, the reel's ``media_url``
    is not a thumbnail — upload manually via the standalone endpoint.
    """
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found")

    video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
    if not video:
        raise HTTPException(status_code=404, detail=f"Video '{video_id}' not found")

    platform = channel.get("platform", "youtube")

    if previous_analysis_id:
        prev = await db.thumbnail_analysis.find_one({"analysis_id": previous_analysis_id})
        if not prev:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Previous analysis '{previous_analysis_id}' not found (may have expired)",
            )

    yt_id = video.get("youtube_video_id")
    if platform == "youtube" and yt_id:
        thumb_url = f"https://img.youtube.com/vi/{yt_id}/maxresdefault.jpg"
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video-linked thumbnail fetch is only supported for YouTube. "
                   "For Instagram, upload the thumbnail image manually via POST /thumbnail-analysis/",
        )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(thumb_url)
        if resp.status_code == 404:
            thumb_url = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"
            resp = await client.get(thumb_url)
        if resp.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to fetch YouTube thumbnail (HTTP {resp.status_code})",
            )
        image_bytes = resp.content

    analysis_id = str(uuid.uuid4())
    now = now_ist()
    title = video.get("title", "Untitled")

    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=".jpg", prefix=f"thumb_{analysis_id}_",
    )
    try:
        tmp.write(image_bytes)
        tmp.flush()
        tmp_path = tmp.name
    finally:
        tmp.close()

    doc: dict[str, Any] = {
        "analysis_id": analysis_id,
        "channel_id": channel_id,
        "title": title,
        "label": label,
        "previous_analysis_id": previous_analysis_id,
        "video_id": video_id,
        "platform": platform,
        "status": "analyzing",
        "analysis": None,
        "error_message": None,
        "created_at": now,
        "analyzed_at": None,
        "expires_at": now + timedelta(hours=_TTL_HOURS),
    }
    await db.thumbnail_analysis.insert_one(doc)

    import app.main as main_mod
    from app.services.thumbnail_analysis import run_thumbnail_analysis

    if not main_mod.gemini_service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    asyncio.create_task(
        run_thumbnail_analysis(
            analysis_id, tmp_path, title, platform, db, main_mod.gemini_service,
        )
    )

    return {
        "ok": True,
        "analysis_id": analysis_id,
        "video_id": video_id,
        "message": "Thumbnail analysis started — poll GET /{analysis_id} for results",
        "expires_at": doc["expires_at"].isoformat(),
    }


# ------------------------------------------------------------------
# GET /{analysis_id} -- get a specific thumbnail analysis
# ------------------------------------------------------------------


@router.get("/{analysis_id}")
async def get_thumbnail_analysis(
    channel_id: str,
    analysis_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get a thumbnail analysis result.

    If the analysis has a ``previous_analysis_id`` and that previous
    analysis still exists, a ``version_comparison`` object is computed
    showing score deltas.
    """
    doc = await db.thumbnail_analysis.find_one(
        {"channel_id": channel_id, "analysis_id": analysis_id}
    )
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Thumbnail analysis '{analysis_id}' not found (may have expired)",
        )

    doc.pop("_id", None)

    version_comparison = None
    prev_id = doc.get("previous_analysis_id")
    if prev_id and doc.get("status") == "completed":
        prev_doc = await db.thumbnail_analysis.find_one({"analysis_id": prev_id})
        if prev_doc:
            from app.services.thumbnail_analysis import compute_thumbnail_comparison
            version_comparison = compute_thumbnail_comparison(doc, prev_doc)

    doc["version_comparison"] = version_comparison
    return doc


# ------------------------------------------------------------------
# GET / -- list active thumbnail analyses for a channel
# ------------------------------------------------------------------


@router.get("/")
async def list_thumbnail_analyses(
    channel_id: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List all active (not yet expired) thumbnail analyses for a channel."""
    docs = await db.thumbnail_analysis.find(
        {"channel_id": channel_id}
    ).sort("created_at", -1).limit(limit).to_list(length=limit)

    for d in docs:
        d.pop("_id", None)

    return docs


# ------------------------------------------------------------------
# DELETE /{analysis_id} -- manually delete a thumbnail analysis
# ------------------------------------------------------------------


@router.delete("/{analysis_id}")
async def delete_thumbnail_analysis(
    channel_id: str,
    analysis_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Manually delete a thumbnail analysis before its TTL expires."""
    result = await db.thumbnail_analysis.delete_one(
        {"channel_id": channel_id, "analysis_id": analysis_id}
    )
    if result.deleted_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Thumbnail analysis '{analysis_id}' not found",
        )
    return {"ok": True, "analysis_id": analysis_id, "deleted": True}
