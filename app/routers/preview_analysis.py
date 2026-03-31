"""Preview analysis router -- ephemeral video retention predictions."""

import asyncio
import os
import tempfile
import uuid
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
from app.logger import get_logger
from app.timezone import now_ist

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/preview-analysis",
    tags=["preview-analysis"],
    dependencies=[Depends(verify_api_key)],
)

_TTL_HOURS = 24


# ------------------------------------------------------------------
# POST / -- upload a video for ephemeral retention analysis
# ------------------------------------------------------------------


@router.post("/", status_code=status.HTTP_202_ACCEPTED)
async def create_preview_analysis(
    channel_id: str,
    file: UploadFile = File(...),
    title: str = Form("Untitled"),
    label: Optional[str] = Form(None),
    previous_preview_id: Optional[str] = Form(None),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Upload a video for ephemeral retention prediction.

    The video is analyzed in the background. Poll ``GET /{preview_id}``
    for results. The analysis auto-expires after 24 hours.
    """
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found")

    if previous_preview_id:
        prev = await db.preview_analysis.find_one({"preview_id": previous_preview_id})
        if not prev:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Previous preview '{previous_preview_id}' not found (may have expired)",
            )

    platform = channel.get("platform", "youtube")
    preview_id = str(uuid.uuid4())
    now = now_ist()

    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=".mp4", prefix=f"preview_{preview_id}_",
    )
    try:
        contents = await file.read()
        tmp.write(contents)
        tmp.flush()
        tmp_path = tmp.name
    finally:
        tmp.close()

    doc: dict[str, Any] = {
        "preview_id": preview_id,
        "channel_id": channel_id,
        "title": title,
        "label": label,
        "previous_preview_id": previous_preview_id,
        "platform": platform,
        "status": "analyzing",
        "analysis": None,
        "error_message": None,
        "created_at": now,
        "analyzed_at": None,
        "expires_at": now + timedelta(hours=_TTL_HOURS),
    }
    await db.preview_analysis.insert_one(doc)

    import app.main as main_mod
    from app.services.preview_analysis import run_preview_analysis

    if not main_mod.gemini_service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gemini service not initialised",
        )

    asyncio.create_task(
        run_preview_analysis(
            preview_id, tmp_path, title, platform, db, main_mod.gemini_service,
        )
    )

    return {
        "ok": True,
        "preview_id": preview_id,
        "message": "Preview analysis started — poll GET /{preview_id} for results",
        "expires_at": doc["expires_at"].isoformat(),
    }


# ------------------------------------------------------------------
# GET /{preview_id} -- get a specific preview analysis
# ------------------------------------------------------------------


@router.get("/{preview_id}")
async def get_preview_analysis(
    channel_id: str,
    preview_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get a preview analysis result.

    If the preview has a ``previous_preview_id`` and that previous
    analysis still exists, a ``version_comparison`` object is computed
    showing score deltas.
    """
    doc = await db.preview_analysis.find_one(
        {"channel_id": channel_id, "preview_id": preview_id}
    )
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Preview '{preview_id}' not found (may have expired)",
        )

    doc.pop("_id", None)

    version_comparison = None
    prev_id = doc.get("previous_preview_id")
    if prev_id and doc.get("status") == "completed":
        prev_doc = await db.preview_analysis.find_one({"preview_id": prev_id})
        if prev_doc:
            from app.services.preview_analysis import compute_version_comparison
            version_comparison = compute_version_comparison(doc, prev_doc)

    doc["version_comparison"] = version_comparison
    return doc


# ------------------------------------------------------------------
# GET / -- list active previews for a channel
# ------------------------------------------------------------------


@router.get("/")
async def list_preview_analyses(
    channel_id: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List all active (not yet expired) preview analyses for a channel."""
    docs = await db.preview_analysis.find(
        {"channel_id": channel_id}
    ).sort("created_at", -1).limit(limit).to_list(length=limit)

    for d in docs:
        d.pop("_id", None)

    return docs


# ------------------------------------------------------------------
# DELETE /{preview_id} -- manually delete a preview
# ------------------------------------------------------------------


@router.delete("/{preview_id}")
async def delete_preview_analysis(
    channel_id: str,
    preview_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Manually delete a preview analysis before its TTL expires."""
    result = await db.preview_analysis.delete_one(
        {"channel_id": channel_id, "preview_id": preview_id}
    )
    if result.deleted_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Preview '{preview_id}' not found",
        )
    return {"ok": True, "preview_id": preview_id, "deleted": True}
