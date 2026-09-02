"""Per-call cost accounting for Gemini requests.

Every Gemini call reports its token usage and what One AI charged for it here,
and this module appends one document to ``db.ai_call_logs`` so spend can be
broken down by model, task, and day.

The rates used to live here, in a ``GEMINI_PRICING`` table kept in step with
Google's published prices by hand. One AI prices calls centrally now, so the
table is gone along with the failure mode it carried: any model missing from it
was priced at ``0.0``, which reads as free and silently understated this app's
spend for as long as the gap went unnoticed. An unpriceable call is stored as
``None`` — unknown, not free.

Writes are best-effort by design: a logging failure must never take down the
call it was measuring.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger

logger = get_logger(__name__)

_bound_db: AsyncIOMotorDatabase | None = None

# Fire-and-forget log tasks are kept here so the event loop cannot garbage
# collect them mid-flight.
_pending_tasks: set[asyncio.Task] = set()


def bind_ai_logger_db(db: AsyncIOMotorDatabase | None) -> None:
    """Set the DB handle used for call-log writes (call from app lifespan)."""

    global _bound_db
    _bound_db = db


async def log_ai_call(
    task: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_ms: float,
    success: bool,
    cost_usd: float | None,
) -> None:
    """Append one call record to ``ai_call_logs``.

    ``cost_usd`` is what One AI charged, and ``None`` when it could not price the
    call. The ``None`` is stored as-is: readers of this collection must treat a
    null as "unknown cost", never coalesce it to zero.

    Silently does nothing when no DB is bound (tests, pre-startup). Any write
    failure is logged and swallowed so the Gemini call path is never affected.
    """

    db = _bound_db
    if db is None:
        return

    doc: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc),
        "task": task,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": cost_usd,
        # Denormalised so a rollup can count unpriced calls without a null test.
        "priced": cost_usd is not None,
        "duration_ms": round(duration_ms, 2),
        "success": success,
    }

    try:
        await db.ai_call_logs.insert_one(doc)
    except Exception as exc:
        logger.error("Failed to write AI call log (task=%s, model=%s): %s", task, model, exc)


def schedule_ai_call_log(
    task: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_ms: float,
    success: bool,
    cost_usd: float | None,
) -> asyncio.Task | None:
    """Persist a call record in the background, off the caller's critical path.

    Returns the task (so tests can await it) or ``None`` when there is no
    running loop to schedule on.
    """

    coro = log_ai_call(task, model, input_tokens, output_tokens, duration_ms, success, cost_usd)
    try:
        task_handle = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return None

    _pending_tasks.add(task_handle)
    task_handle.add_done_callback(_pending_tasks.discard)
    return task_handle
