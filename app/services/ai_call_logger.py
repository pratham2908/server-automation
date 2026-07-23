"""Per-call cost accounting for Gemini requests.

Every Gemini call reports its token usage here; this module prices it against
current Vertex AI rates and appends one document to ``db.ai_call_logs`` so spend
can be broken down by model, task, and day.

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

# Vertex AI standard-tier pricing, USD per 1M tokens (fetched 2026-07-24).
# Models with a ``context_threshold`` bill at the ``_long`` rates once a prompt
# exceeds that many input tokens.
GEMINI_PRICING: dict[str, dict[str, float]] = {
    "gemini-2.5-pro": {
        "input_per_1m": 1.25,
        "output_per_1m": 10.00,
        "input_per_1m_long": 2.50,
        "output_per_1m_long": 15.00,
        "context_threshold": 200_000,
    },
    "gemini-2.5-flash": {
        "input_per_1m": 0.30,
        "output_per_1m": 2.50,
    },
    "gemini-2.5-flash-lite": {
        "input_per_1m": 0.10,
        "output_per_1m": 0.40,
    },
    # Preview model with no published pricing — proxied to 2.5-flash rates so
    # spend is approximated rather than silently dropped.
    "gemini-3-flash-preview": {
        "input_per_1m": 0.30,
        "output_per_1m": 2.50,
    },
}

_PER_MILLION = 1_000_000

_bound_db: AsyncIOMotorDatabase | None = None

# Fire-and-forget log tasks are kept here so the event loop cannot garbage
# collect them mid-flight.
_pending_tasks: set[asyncio.Task] = set()


def bind_ai_logger_db(db: AsyncIOMotorDatabase | None) -> None:
    """Set the DB handle used for call-log writes (call from app lifespan)."""

    global _bound_db
    _bound_db = db


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return the USD cost of one call, or ``0.0`` if the model has no known pricing."""

    pricing = GEMINI_PRICING.get(model)
    if pricing is None:
        logger.warning("No pricing for model '%s' — recording call at $0.00", model)
        return 0.0

    threshold = pricing.get("context_threshold")
    use_long_rates = threshold is not None and input_tokens > threshold

    if use_long_rates:
        input_rate = pricing["input_per_1m_long"]
        output_rate = pricing["output_per_1m_long"]
    else:
        input_rate = pricing["input_per_1m"]
        output_rate = pricing["output_per_1m"]

    return (input_tokens * input_rate + output_tokens * output_rate) / _PER_MILLION


async def log_ai_call(
    task: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_ms: float,
    success: bool,
) -> None:
    """Append one call record to ``ai_call_logs``.

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
        "cost_usd": compute_cost(model, input_tokens, output_tokens),
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
) -> asyncio.Task | None:
    """Persist a call record in the background, off the caller's critical path.

    Returns the task (so tests can await it) or ``None`` when there is no
    running loop to schedule on.
    """

    coro = log_ai_call(task, model, input_tokens, output_tokens, duration_ms, success)
    try:
        task_handle = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return None

    _pending_tasks.add(task_handle)
    task_handle.add_done_callback(_pending_tasks.discard)
    return task_handle
