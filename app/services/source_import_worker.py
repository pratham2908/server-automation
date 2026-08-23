"""Background worker that pulls a channel app's rendered videos into our R2.

One job at a time. Transfers are network-bound and renders are small, so a single
sequential worker keeps disk and bandwidth predictable without needing a pool.

Deliberately separate from the batch analysis worker: getting the file into the
system should not queue behind a long AI analysis. Once a transfer lands, the job
hands off to ``batch_analysis_queue`` and the existing analysis worker takes over.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.services.error_reporting import report_error
from app.services.r2 import R2Service
from app.services.video_source_service import VideoSourceService, get_import_queue
from app.services.video_sources import adapter_for, describe_http_error
from app.timezone import now_ist

logger = get_logger(__name__)

DOWNLOAD_TIMEOUT_S = 900.0
CHUNK_BYTES = 1024 * 1024

# Renders run to hundreds of MB, and the analysis worker later needs its own copy
# of the same file. Refuse a transfer that would leave the disk this close to full
# rather than filling it and taking unrelated writes down with us.
DISK_HEADROOM_BYTES = 1024 * 1024 * 1024  # 1 GB


async def run_source_import_worker(db: AsyncIOMotorDatabase, r2: R2Service) -> None:
    """Drain the import queue forever, transferring one video at a time."""
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
                # Safety net for a put_nowait lost to a crash between the DB write
                # and the in-memory queue.
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
    """Transfer one rendered video into our R2, then hand off to analysis."""
    now = now_ist()

    # Atomically claim the job so a duplicate queue entry cannot double-transfer.
    job = await db.source_imports.find_one_and_update(
        {"job_id": job_id, "status": "queued"},
        {"$set": {"status": "transferring", "started_at": now, "message": "Requesting download link…"}},
        return_document=True,
    )
    if not job:
        return

    channel_id = job["channel_id"]
    source_id = job["source_id"]
    source_video_id = job["source_video_id"]
    r2_key = job["r2_key"]
    video_id = job["video_id"]
    title = job.get("title") or source_video_id

    tmp_path: str | None = None
    # Tracks whether the file reached our R2. Once it has, a later failure (e.g.
    # enqueuing analysis) must not delete the record — the video really exists.
    file_landed = False
    try:
        service = VideoSourceService(db)
        source = await service.load_source(source_id)

        # ── 1. Mint a fresh download URL ────────────────────────────────
        # Always fetched here rather than at browse time: these URLs expire, and a
        # backed-up queue or a retry can easily outlive one minted earlier. How it
        # is minted is the adapter's problem, not ours.
        download_url = await service.download_url(source, source_video_id)

        # ── 2. Stream the app's URL to a local temp file ────────────────
        await db.source_imports.update_one(
            {"job_id": job_id},
            {"$set": {"message": f"Downloading {title}…"}},
        )
        tmp_path, size_bytes = await asyncio.to_thread(_stream_to_temp, download_url)

        await db.source_imports.update_one(
            {"job_id": job_id},
            {"$set": {"size_bytes": size_bytes, "message": f"Uploading {title} to storage…"}},
        )

        # ── 3. Local temp file → our R2 ─────────────────────────────────
        download_path: str = tmp_path  # type: ignore

        def _upload() -> None:
            with open(download_path, "rb") as fh:
                r2.upload_video(fh, r2_key)

        await asyncio.to_thread(_upload)

        os.unlink(download_path)
        tmp_path = None
        file_landed = True

        # ── 4. File is in the system ────────────────────────────────────
        # The file is in R2, but not yet postable: AI packaging writes the
        # title/description/tags. So it stays "processing" while analysis runs
        # (run_retention_analysis promotes it to "ready" on success). When
        # analysis is skipped there is nothing more to wait for, so it is ready now.
        will_analyze = job.get("analyze", True)
        if not will_analyze:
            await db.videos.update_one(
                {"video_id": video_id},
                {"$set": {"status": "ready", "updated_at": now_ist()}},
            )

        # ── 5. Close the pull loop with the app ─────────────────────────
        # Deliberately fired on ingest, not after analysis: we hold the file now,
        # and analysis can fail or be retried for reasons that have nothing to do
        # with delivery. Waiting for it would leave the app believing the render
        # was never delivered, inviting a duplicate push.
        notify_error = await service.notify_imported(source, source_video_id, video_id)
        if notify_error:
            # Non-fatal either way — the video really is here, and the job records
            # the reason. It only reaches the error queue for an app that can push
            # to us, where a missed callback really can produce a duplicate our
            # dedup cannot see. For a pull-only app it would just be noise on every
            # import until the app ships the field.
            logger.warning("Import callback failed for video %s: %s", source_video_id, notify_error)
            if adapter_for(source).pushes_to_us:
                await report_error(
                    feature="Source import callback",
                    message=(
                        f"Ingested '{source_video_id}' but could not notify the app: {notify_error}. "
                        f"The app may re-deliver it and create a duplicate of video {video_id}."
                    ),
                    exception=None,
                    context={
                        "job_id": job_id,
                        "source_id": source_id,
                        "source_video_id": source_video_id,
                        "video_id": video_id,
                    },
                )
        await db.source_imports.update_one(
            {"job_id": job_id},
            {"$set": {"notified_at": None if notify_error else now_ist(), "notify_error": notify_error}},
        )

        # ── 6. Hand off to the existing analysis pipeline ───────────────
        if will_analyze:
            await _enqueue_for_analysis(db, job)
            message = "Imported — queued for AI analysis"
            if notify_error:
                message += " (app not notified)"
        else:
            await db.videos.update_one(
                {"video_id": video_id},
                {"$set": {"packaging_status": "skipped", "updated_at": now_ist()}},
            )
            message = "Imported"

        await db.source_imports.update_one(
            {"job_id": job_id},
            {"$set": {"status": "completed", "completed_at": now_ist(), "message": message}},
        )
        logger.success("Imported render %s → %s (%s bytes)", source_video_id, r2_key, size_bytes)

    except Exception as exc:
        detail_msg = describe_http_error(exc) if isinstance(exc, httpx.HTTPError) else str(exc)
        logger.error("Import job %s failed: %s", job_id, detail_msg, exc_info=True)
        await report_error(
            feature="Source import worker",
            message=f"Failed to import render '{source_video_id}' for channel {channel_id}: {detail_msg}",
            exception=exc,
            context={"job_id": job_id, "source_id": source_id, "source_video_id": source_video_id},
        )
        await db.source_imports.update_one(
            {"job_id": job_id},
            {"$set": {"status": "failed", "message": detail_msg, "completed_at": now_ist()}},
        )
        # Remove the placeholder video record — a half-imported video with no file
        # behind it is worse than no record, and it would otherwise block a retry
        # via the already-imported dedup check. Only when the file never landed, so
        # a video already transferred (and merely awaiting analysis) is left alone.
        if not file_landed:
            await db.videos.delete_one({"video_id": video_id})
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError as exc:
                logger.debug("Could not remove temp file '%s': %s", tmp_path, exc)


def _stream_to_temp(url: str) -> tuple[str, int]:
    """Stream *url* to a temp file. Returns (path, bytes written).

    Blocking on purpose — the caller runs it in a thread. Uses a streaming read so
    a large render never sits fully in memory.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
        tmp_path = tmp.name

    try:
        written = 0
        with httpx.stream("GET", url, timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True) as resp:
            resp.raise_for_status()

            expected = int(resp.headers.get("content-length") or 0)
            free = shutil.disk_usage(os.path.dirname(tmp_path)).free
            if expected and free < expected + DISK_HEADROOM_BYTES:
                raise OSError(
                    f"Not enough disk to import this render: needs {expected / 1e6:.0f} MB "
                    f"plus {DISK_HEADROOM_BYTES / 1e9:.0f} GB headroom, only {free / 1e6:.0f} MB free"
                )

            with open(tmp_path, "wb") as fh:
                for chunk in resp.iter_bytes(CHUNK_BYTES):
                    fh.write(chunk)
                    written += len(chunk)
    except Exception:
        # Never leave a partial file behind for the caller to mistake for a success.
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    if written == 0:
        os.unlink(tmp_path)
        raise ValueError("Download produced an empty file")

    return tmp_path, written


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
            "filename": job.get("title") or job["source_video_id"],
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
