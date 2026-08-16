"""Background worker that pulls source-bucket objects into our own R2.

One job at a time. Transfers are network-bound and reels are small, so a single
sequential worker keeps disk and bandwidth predictable without needing a pool.

Deliberately separate from the batch analysis worker: getting the file into the
system should not queue behind a long AI analysis. Once a transfer lands, the job
hands off to ``batch_analysis_queue`` and the existing analysis worker takes over.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.error_reporting import report_error
from app.services.r2 import R2Service
from app.services.video_source_service import build_client, get_import_queue
from app.timezone import now_ist

logger = get_logger(__name__)


async def run_source_import_worker(db: AsyncIOMotorDatabase, r2: R2Service) -> None:
    """Drain the import queue forever, transferring one object at a time."""
    logger.info("Source import worker started")

    # Recover anything left mid-flight by a restart. "transferring" jobs never
    # resumed on their own, so they go back to the queue rather than sit stuck.
    stale = await db.source_imports.find({"status": {"$in": ["queued", "transferring"]}}).to_list(length=None)
    queue = get_import_queue()
    for job in stale:
        if job["status"] == "transferring":
            await db.source_imports.update_one(
                {"job_id": job["job_id"]},
                {"$set": {"status": "queued", "message": "Requeued after restart"}},
            )
        queue.put_nowait(job["job_id"])
    if stale:
        logger.info("Requeued %d stale import job(s) after restart", len(stale))

    while True:
        try:
            try:
                job_id = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Safety net for a put_nowait that was lost to a crash between
                # the DB write and the in-memory queue.
                missed = await db.source_imports.find_one({"status": "queued"}, sort=[("created_at", 1)])
                if not missed:
                    continue
                job_id = missed["job_id"]

            await _process_import(job_id, db, r2)

        except asyncio.CancelledError:
            logger.info("Source import worker shutting down")
            raise
        except Exception as exc:
            logger.error("Source import worker unhandled error: %s", exc, exc_info=True)
            await asyncio.sleep(5)


async def _process_import(job_id: str, db: AsyncIOMotorDatabase, r2: R2Service) -> None:
    """Transfer one source object into our R2, then hand off to analysis."""
    now = now_ist()

    # Atomically claim the job so a duplicate queue entry cannot double-transfer.
    job = await db.source_imports.find_one_and_update(
        {"job_id": job_id, "status": "queued"},
        {"$set": {"status": "transferring", "started_at": now, "message": "Connecting to source…"}},
        return_document=True,
    )
    if not job:
        return

    channel_id = job["channel_id"]
    source_id = job["source_id"]
    source_key = job["source_object_key"]
    r2_key = job["r2_key"]
    video_id = job["video_id"]
    filename = job["filename"]

    tmp_path: str | None = None
    try:
        source = await db.video_sources.find_one({"source_id": source_id})
        if not source:
            raise ValueError(f"Video source '{source_id}' no longer exists")

        client = build_client(source)

        # ── 1. Size the object so the UI can show something meaningful ──
        meta = await asyncio.to_thread(client.head_object, source_key)
        size_bytes = meta["size"]
        await db.source_imports.update_one(
            {"job_id": job_id},
            {"$set": {"size_bytes": size_bytes, "message": f"Downloading {filename}…"}},
        )

        # ── 2. Source bucket → local temp file ──────────────────────────
        suffix = os.path.splitext(source_key)[1] or ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
        # Non-optional alias so the upload closure below captures a plain str.
        download_path: str = tmp_path
        await asyncio.to_thread(client.download_to_path, source_key, download_path)

        # ── 3. Local temp file → our R2 ─────────────────────────────────
        await db.source_imports.update_one(
            {"job_id": job_id},
            {"$set": {"message": f"Uploading {filename} to storage…"}},
        )

        def _upload() -> None:
            with open(download_path, "rb") as fh:
                r2.upload_video(fh, r2_key)

        await asyncio.to_thread(_upload)

        os.unlink(download_path)
        tmp_path = None

        # ── 4. File is in the system — the video is now usable ──────────
        await db.videos.update_one(
            {"video_id": video_id},
            {"$set": {"status": "ready", "updated_at": now_ist()}},
        )

        # ── 5. Hand off to the existing analysis pipeline ───────────────
        if job.get("analyze", True):
            await _enqueue_for_analysis(db, job)
            message = "Imported — queued for AI analysis"
        else:
            await db.videos.update_one(
                {"video_id": video_id},
                {"$set": {"packaging_status": "skipped", "updated_at": now_ist()}},
            )
            message = "Imported"

        await db.source_imports.update_one(
            {"job_id": job_id},
            {
                "$set": {
                    "status": "completed",
                    "bytes_transferred": size_bytes,
                    "completed_at": now_ist(),
                    "message": message,
                }
            },
        )
        logger.success("Imported %s → %s (%s bytes)", source_key, r2_key, size_bytes)

    except Exception as exc:
        logger.error("Import job %s failed: %s", job_id, exc, exc_info=True)
        await report_error(
            feature="Source import worker",
            message=f"Failed to import '{source_key}' for channel {channel_id}: {exc!s}",
            exception=exc,
            context={"job_id": job_id, "source_id": source_id, "source_object_key": source_key},
        )
        await db.source_imports.update_one(
            {"job_id": job_id},
            {"$set": {"status": "failed", "message": str(exc), "completed_at": now_ist()}},
        )
        # Remove the placeholder video record — a half-imported video with no
        # file behind it is worse than no record at all, and it would otherwise
        # block a retry via the already-imported dedup check.
        await db.videos.delete_one({"video_id": video_id, "status": "uploading"})
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError as exc:
                logger.debug("Could not remove temp file '%s': %s", tmp_path, exc)


async def _enqueue_for_analysis(db: AsyncIOMotorDatabase, job: dict[str, Any]) -> None:
    """Push a completed import onto the shared batch analysis queue."""
    from app.services.batch_upload_service import get_analysis_queue

    now = now_ist()
    file_id = job["job_id"]  # reuse the job id as the analysis queue's file id

    last_queued = await db.batch_analysis_queue.find_one(
        {"status": {"$in": ["queued", "analyzing"]}},
        sort=[("position", -1)],
    )
    position = (last_queued["position"] if last_queued else 0) + 1

    await db.batch_analysis_queue.insert_one(
        {
            "batch_id": f"import:{job['source_id']}",
            "file_id": file_id,
            "filename": job["filename"],
            "r2_key": job["r2_key"],
            "primary_channel_id": job["channel_id"],
            "channel_video_ids": [{"channel_id": job["channel_id"], "video_id": job["video_id"]}],
            "scheduled_at": job.get("scheduled_at"),
            "status": "queued",
            "position": position,
            "message": f"Queued — position {position}",
            "created_at": now,
            "started_at": None,
            "completed_at": None,
        }
    )
    await db.videos.update_one(
        {"video_id": job["video_id"]},
        {"$set": {"packaging_status": "pending", "updated_at": now}},
    )
    get_analysis_queue().put_nowait(file_id)
