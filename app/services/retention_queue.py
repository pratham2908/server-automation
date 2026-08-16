"""Durable, sequential queue for retention-analysis ("predict") triggers.

Previously each API trigger called ``create_monitored_task(run_retention_analysis)``
and returned immediately, so N triggers meant N concurrent R2 downloads + Gemini
video calls racing on the event loop — a stampede that hits quota and memory limits.

This module serialises them: a trigger enqueues a job into the durable
``retention_analysis_queue`` collection and a single background worker
(:func:`run_retention_analysis_worker`) drains it one at a time. The pattern
mirrors the existing batch analysis worker: a Mongo-backed queue for durability
(survives restarts) plus an in-process wake-up signal so the worker reacts
immediately instead of only on its poll interval.
"""

from __future__ import annotations

import asyncio
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from app.logger import get_logger
from app.timezone import now_ist

logger = get_logger(__name__)

# A job is "pending in the queue" while its status is one of these.
_ACTIVE_STATES = ["queued", "analyzing"]

# Fallback poll interval: even if a wake-up signal is missed, the worker rechecks
# the DB this often. Keeps the queue draining after restarts / cross-process
# enqueues where the in-memory signal never fired.
_POLL_SECONDS = 30.0

# In-process wake-up pulse. Carries no payload — it only tells the worker "there
# may be new work, look now". The DB is the source of truth for what to run.
_signal: asyncio.Queue[int] | None = None


def _get_signal() -> asyncio.Queue[int]:
    global _signal
    if _signal is None:
        _signal = asyncio.Queue()
    return _signal


async def enqueue_retention_analysis(db: AsyncIOMotorDatabase, channel_id: str, video_id: str) -> dict[str, Any]:
    """Add a retention-analysis job for one video, unless one is already pending.

    Returns ``{queued, already_queued, position}``. Deduplicated: a video that
    already has a queued or in-flight job is not enqueued twice.
    """
    existing = await db.retention_analysis_queue.find_one(
        {"channel_id": channel_id, "video_id": video_id, "status": {"$in": _ACTIVE_STATES}}
    )
    if existing:
        return {"queued": False, "already_queued": True, "position": existing.get("position")}

    last = await db.retention_analysis_queue.find_one(
        {"status": {"$in": _ACTIVE_STATES}},
        sort=[("position", -1)],
    )
    position = (last["position"] + 1) if last else 1

    now = now_ist()
    await db.retention_analysis_queue.insert_one(
        {
            "channel_id": channel_id,
            "video_id": video_id,
            "status": "queued",
            "position": position,
            "created_at": now,
            "updated_at": now,
        }
    )

    # Reflect the wait on the video so the UI shows "pending" the instant the
    # trigger returns, before the worker picks it up.
    await db.videos.update_one(
        {"channel_id": channel_id, "video_id": video_id},
        {"$set": {"retention.status": "pending", "retention.updated_at": now, "updated_at": now}},
    )

    _wake_worker()
    return {"queued": True, "already_queued": False, "position": position}


def _wake_worker() -> None:
    """Pulse the worker if an event loop is running; harmless otherwise."""
    try:
        _get_signal().put_nowait(1)
    except (RuntimeError, asyncio.QueueFull):
        # No running loop (e.g. tests) or a pulse already pending — the worker's
        # poll fallback will still pick the job up.
        pass


async def claim_next_job(db: AsyncIOMotorDatabase) -> dict[str, Any] | None:
    """Atomically claim the oldest queued job, flipping it to ``analyzing``.

    A single ``find_one_and_update`` sorted by position is the claim, so even if
    two workers ever ran, each job is handed out once.
    """
    job: dict[str, Any] | None = await db.retention_analysis_queue.find_one_and_update(
        {"status": "queued"},
        {"$set": {"status": "analyzing", "started_at": now_ist()}},
        sort=[("position", 1)],
        return_document=ReturnDocument.AFTER,
    )
    return job


async def recover_stale_jobs(db: AsyncIOMotorDatabase) -> int:
    """Reset jobs stuck in ``analyzing`` (from a crash mid-run) back to ``queued``.

    Returns how many were recovered. Run once at worker startup.
    """
    stale = await (db.retention_analysis_queue.find({"status": "analyzing"})).to_list(length=None)
    for job in stale:
        await db.retention_analysis_queue.update_one(
            {"_id": job["_id"]},
            {"$set": {"status": "queued", "updated_at": now_ist()}},
        )
    if stale:
        logger.info("Recovered %d stale retention job(s) after restart", len(stale))
    return len(stale)


async def run_retention_analysis_worker(db: AsyncIOMotorDatabase, r2: Any, gemini: Any) -> None:
    """Drain the retention queue one job at a time, forever.

    ``run_retention_analysis`` is self-contained — it records its own
    ``retention.status`` (analyzing → completed/failed) and never raises out — so
    the worker only owns queue bookkeeping: claim, run, remove.
    """
    from app.services.retention_analysis import run_retention_analysis

    logger.info("Retention analysis worker started")
    await recover_stale_jobs(db)

    while True:
        try:
            job = await claim_next_job(db)
            if job is None:
                # Nothing queued — sleep until a pulse arrives or the poll fires.
                try:
                    await asyncio.wait_for(_get_signal().get(), timeout=_POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
                continue

            try:
                await run_retention_analysis(job["channel_id"], job["video_id"], db, r2, gemini)
            finally:
                # Remove the ticket whether the analysis succeeded or failed; the
                # outcome already lives on the video's retention.status.
                await db.retention_analysis_queue.delete_one({"_id": job["_id"]})

        except asyncio.CancelledError:
            logger.info("Retention analysis worker shutting down")
            raise
        except Exception as exc:
            logger.error("Retention worker unhandled error: %s", exc, exc_info=True)
            await asyncio.sleep(5)
