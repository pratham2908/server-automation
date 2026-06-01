"""Batch upload service.

Implements the three-step direct-to-R2 upload flow:

1. batch_init   — generate presigned PUT URLs, create video records in "uploading" status
2. batch_confirm — mark uploads done, enqueue for sequential AI analysis
3. get_batch_status — poll progress

Plus a long-running singleton worker (run_batch_analysis_worker) that processes
the analysis queue one video at a time, so memory and Gemini rate-limits are
never strained regardless of batch size.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_channel_platform
from app.logger import get_logger
from app.services.error_reporting import report_error
from app.services.gemini import GeminiService
from app.services.r2 import R2Service
from app.services.retention_analysis import extract_thumbnail, run_retention_analysis
from app.timezone import now_ist

logger = get_logger(__name__)

# ── Analysis queue (in-process asyncio queue, backed by DB for durability) ──
_analysis_queue: asyncio.Queue[str] | None = None  # queue of batch_analysis_queue._id strings


def get_analysis_queue() -> asyncio.Queue[str]:
    global _analysis_queue
    if _analysis_queue is None:
        _analysis_queue = asyncio.Queue()
    return _analysis_queue


class BatchUploadService:
    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        r2: R2Service | None = None,
        gemini: GeminiService | None = None,
    ) -> None:
        self.db = db
        self.r2 = r2
        self.gemini = gemini

    # ------------------------------------------------------------------
    # 1. batch_init
    # ------------------------------------------------------------------
    async def batch_init(
        self,
        primary_channel_id: str,
        files: list[dict],
        scheduled_at: str | None = None,
    ) -> dict[str, Any]:
        """Create DB records + presigned PUT URLs for a batch of files.

        Parameters
        ----------
        primary_channel_id:
            The channel that "owns" the upload (used as the R2 key prefix).
        files:
            List of ``{"filename": str, "size_bytes": int, "channels": [channel_id, ...]}``.
        scheduled_at:
            Optional ISO datetime to schedule all videos to.

        Returns
        -------
        ``{"batch_id": str, "uploads": [{file_id, filename, upload_url, r2_key,
           channel_video_ids: [{channel_id, video_id}]}]}``
        """
        assert self.r2 is not None, "R2 service not initialised"

        batch_id = str(uuid.uuid4())
        now = now_ist()
        uploads: list[dict] = []
        batch_file_ids: list[str] = []

        for f in files:
            file_id = str(uuid.uuid4())
            r2_key = f"{primary_channel_id}/{file_id}.mp4"

            # Presigned PUT URL (browser uploads directly to R2)
            upload_url = self.r2.generate_presigned_put_url(r2_key, expires_in=900)

            channel_ids: list[str] = f.get("channels") or [primary_channel_id]
            channel_video_ids: list[dict] = []

            for cid in channel_ids:
                channel = await self.db.channels.find_one({"channel_id": cid})
                if not channel:
                    continue
                vid_id = str(uuid.uuid4())
                doc = {
                    "channel_id": cid,
                    "video_id": vid_id,
                    "title": f.get("filename", "Untitled").rsplit(".", 1)[0],
                    "description": "",
                    "tags": [],
                    "category": "Uncategorized",
                    "status": "uploading",
                    "r2_object_key": r2_key,
                    "multi_channel_group_id": file_id,  # file_id acts as group for this file
                    "packaging_status": "pending",
                    "created_at": now,
                    "updated_at": now,
                }
                await self.db.videos.insert_one(doc)
                channel_video_ids.append({"channel_id": cid, "video_id": vid_id})

            # Track in batch_analysis_queue (status: uploading until confirmed)
            await self.db.batch_analysis_queue.insert_one({
                "batch_id": batch_id,
                "file_id": file_id,
                "filename": f.get("filename", "video.mp4"),
                "r2_key": r2_key,
                "primary_channel_id": primary_channel_id,
                "channel_video_ids": channel_video_ids,
                "scheduled_at": scheduled_at,
                "status": "uploading",
                "position": len(batch_file_ids) + 1,
                "message": None,
                "created_at": now,
                "started_at": None,
                "completed_at": None,
            })

            uploads.append({
                "file_id": file_id,
                "filename": f.get("filename", "video.mp4"),
                "upload_url": upload_url,
                "r2_key": r2_key,
                "channel_video_ids": channel_video_ids,
            })
            batch_file_ids.append(file_id)

        # Store batch record
        await self.db.batch_uploads.insert_one({
            "batch_id": batch_id,
            "primary_channel_id": primary_channel_id,
            "file_ids": batch_file_ids,
            "created_at": now,
        })

        return {"batch_id": batch_id, "uploads": uploads}

    # ------------------------------------------------------------------
    # 2. batch_confirm
    # ------------------------------------------------------------------
    async def batch_confirm(
        self,
        batch_id: str,
        confirmed_file_ids: list[str],
    ) -> dict[str, Any]:
        """Mark file uploads as done and enqueue them for AI analysis."""
        now = now_ist()

        # Compute global queue position: put confirmed items after anything already queued
        last_queued = await self.db.batch_analysis_queue.find_one(
            {"status": {"$in": ["queued", "analyzing"]}},
            sort=[("position", -1)],
        )
        base_position = (last_queued["position"] if last_queued else 0)

        queued_count = 0
        for idx, file_id in enumerate(confirmed_file_ids):
            result = await self.db.batch_analysis_queue.find_one_and_update(
                {"batch_id": batch_id, "file_id": file_id, "status": "uploading"},
                {"$set": {
                    "status": "queued",
                    "position": base_position + idx + 1,
                    "message": f"Queued — position {base_position + idx + 1}",
                    "updated_at": now,
                }},
            )
            if result:
                # Update video records to "ready" (upload done, waiting for analysis)
                for cv in result.get("channel_video_ids", []):
                    await self.db.videos.update_one(
                        {"video_id": cv["video_id"]},
                        {"$set": {"status": "ready", "packaging_status": "pending", "updated_at": now}},
                    )
                # Signal the worker
                get_analysis_queue().put_nowait(file_id)
                queued_count += 1

        return {"ok": True, "queued": queued_count}

    # ------------------------------------------------------------------
    # 3. get_batch_status
    # ------------------------------------------------------------------
    async def get_batch_status(self, batch_id: str) -> dict[str, Any]:
        """Return current processing status for a batch."""
        items_cursor = self.db.batch_analysis_queue.find(
            {"batch_id": batch_id},
            sort=[("position", 1)],
        )
        items = await items_cursor.to_list(length=None)

        status_counts: dict[str, int] = {}
        serialised = []
        for it in items:
            s = it["status"]
            status_counts[s] = status_counts.get(s, 0) + 1
            serialised.append({
                "file_id": it["file_id"],
                "filename": it["filename"],
                "status": s,
                "position": it["position"],
                "message": it.get("message"),
                "channel_video_ids": it.get("channel_video_ids", []),
            })

        return {
            "batch_id": batch_id,
            "total": len(items),
            "uploading": status_counts.get("uploading", 0),
            "queued": status_counts.get("queued", 0),
            "analyzing": status_counts.get("analyzing", 0),
            "completed": status_counts.get("completed", 0),
            "failed": status_counts.get("failed", 0),
            "items": serialised,
        }


# ------------------------------------------------------------------
# Sequential analysis worker (singleton, started once in main.py)
# ------------------------------------------------------------------

async def run_batch_analysis_worker(
    db: AsyncIOMotorDatabase,
    r2: R2Service,
    gemini: GeminiService,
) -> None:
    """Process the batch analysis queue one video at a time.

    On startup, requeues any items left in "queued" state from a previous run
    (handles server restarts gracefully).  Then loops, picking the next item,
    running the full retention + packaging analysis, and updating all sibling
    video records.
    """
    logger.info("Batch analysis worker started")

    # Recover items left in "queued" state from a previous run
    stale = await db.batch_analysis_queue.find(
        {"status": {"$in": ["queued", "analyzing"]}},
        sort=[("position", 1)],
    ).to_list(length=None)
    queue = get_analysis_queue()
    for item in stale:
        # Reset stuck "analyzing" items back to queued
        if item["status"] == "analyzing":
            await db.batch_analysis_queue.update_one(
                {"_id": item["_id"]},
                {"$set": {"status": "queued", "message": "Requeued after restart"}},
            )
        queue.put_nowait(item["file_id"])
    if stale:
        logger.info("Requeued %d stale batch items after restart", len(stale))

    while True:
        try:
            # Block until there's work, check every 30s as fallback
            try:
                file_id = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Poll DB for anything we might have missed
                missed = await db.batch_analysis_queue.find_one(
                    {"status": "queued"},
                    sort=[("position", 1)],
                )
                if not missed:
                    continue
                file_id = missed["file_id"]

            await _process_batch_item(file_id, db, r2, gemini)

        except asyncio.CancelledError:
            logger.info("Batch analysis worker shutting down")
            raise
        except Exception as exc:
            logger.error("Batch worker unhandled error: %s", exc, exc_info=True)
            await asyncio.sleep(5)


async def _process_batch_item(
    file_id: str,
    db: AsyncIOMotorDatabase,
    r2: R2Service,
    gemini: GeminiService,
) -> None:
    """Run the full analysis pipeline for one batch item."""
    now = now_ist()

    # Atomically claim the item
    item = await db.batch_analysis_queue.find_one_and_update(
        {"file_id": file_id, "status": "queued"},
        {"$set": {"status": "analyzing", "started_at": now, "message": "Downloading video…"}},
        return_document=True,
    )
    if not item:
        return  # Already processed or claimed

    r2_key = item["r2_key"]
    primary_channel_id = item["primary_channel_id"]
    channel_video_ids: list[dict] = item.get("channel_video_ids", [])
    filename = item.get("filename", "video.mp4")

    logger.info("Processing batch item %s (%s)", file_id, filename)

    temp_path: str | None = None
    try:
        # ── 1. Download from R2 ──────────────────────────────────────
        await db.batch_analysis_queue.update_one(
            {"file_id": file_id},
            {"$set": {"message": f"Downloading {filename}…"}},
        )
        temp_path = r2.download_video(r2_key)

        # ── 2. Run Gemini analysis on the primary channel's video ───
        primary_cv = next(
            (cv for cv in channel_video_ids if cv["channel_id"] == primary_channel_id),
            channel_video_ids[0] if channel_video_ids else None,
        )
        if not primary_cv:
            raise ValueError("No channel video records found")

        primary_video_id = primary_cv["video_id"]
        primary_channel = await db.channels.find_one({"channel_id": primary_channel_id})
        primary_platform = (primary_channel or {}).get("platform", "youtube")

        await db.batch_analysis_queue.update_one(
            {"file_id": file_id},
            {"$set": {"message": f"Analysing {filename} with AI…"}},
        )
        # Mark all channel records as "analyzing"
        for cv in channel_video_ids:
            await db.videos.update_one(
                {"video_id": cv["video_id"]},
                {"$set": {"packaging_status": "analyzing", "updated_at": now}},
            )

        # Full retention analysis on primary video (this also writes packaging to DB)
        await run_retention_analysis(
            primary_channel_id,
            primary_video_id,
            db,
            r2,
            gemini,
            local_video_path=temp_path,
        )

        # run_retention_analysis already propagates packaging to siblings via
        # multi_channel_group_id. For items whose group_id == file_id, this
        # is handled automatically.

        # ── 3. Delete temp file immediately ─────────────────────────
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
            temp_path = None

        # ── 4. Mark complete ─────────────────────────────────────────
        await db.batch_analysis_queue.update_one(
            {"file_id": file_id},
            {"$set": {
                "status": "completed",
                "completed_at": now_ist(),
                "message": "Analysis complete",
            }},
        )
        logger.success("Batch item %s completed (%s)", file_id, filename)

    except Exception as exc:
        logger.error("Batch item %s failed: %s", file_id, exc, exc_info=True)
        await report_error(
            feature="Batch analysis worker",
            message=f"Failed to process batch item {file_id}: {exc!s}",
            exception=exc,
            context={"file_id": file_id, "r2_key": r2_key},
        )
        await db.batch_analysis_queue.update_one(
            {"file_id": file_id},
            {"$set": {"status": "failed", "message": str(exc), "completed_at": now_ist()}},
        )
        for cv in channel_video_ids:
            await db.videos.update_one(
                {"video_id": cv["video_id"]},
                {"$set": {"packaging_status": "failed", "updated_at": now_ist()}},
            )
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                pass
