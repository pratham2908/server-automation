"""Route operational failures into the Mongo-backed error queue.

Used so failures in background tasks and swallowed exceptions still surface in
``/api/errors`` when you review the app later (not only uncaught HTTP 500s).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.errors import get_error_service

logger = logging.getLogger(__name__)

_bound_db: AsyncIOMotorDatabase | None = None


def bind_error_queue_db(db: AsyncIOMotorDatabase | None) -> None:
    """Set the DB handle used for queue writes (call from app lifespan)."""

    global _bound_db
    _bound_db = db


def get_bound_db() -> AsyncIOMotorDatabase | None:
    return _bound_db


async def report_error(
    feature: str,
    message: str,
    exception: BaseException | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """Persist an error to the queue; falls back to logging if DB is unavailable."""

    db = _bound_db
    if db is None:
        logger.error("[error queue unavailable] %s: %s", feature, message, exc_info=exception)
        return
    exc = exception if isinstance(exception, Exception) else None
    try:
        await get_error_service(db).log_error(
            feature=feature,
            message=message,
            exception=exc,
            context=context,
        )
    except Exception as e:
        logger.error("Failed to write to error queue: %s (original: %s — %s)", e, feature, message)


def create_monitored_task(
    coro,
    *,
    feature: str,
    context: dict[str, Any] | None = None,
) -> asyncio.Task:
    """Like ``asyncio.create_task``, but failures are also written to the error queue."""

    loop = asyncio.get_running_loop()
    task = loop.create_task(coro)

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is None:
            return
        db = _bound_db
        if db is None:
            logger.error("%s: background task failed (no DB bound): %s", feature, exc)
            return

        async def _flush() -> None:
            try:
                await get_error_service(db).log_error(
                    feature=feature,
                    message=f"Background task failed: {exc!s}",
                    exception=exc if isinstance(exc, Exception) else None,
                    context=context,
                )
            except Exception as log_err:
                logger.error("Failed to log background task error: %s", log_err)

        try:
            loop.create_task(_flush())
        except RuntimeError:
            pass

    task.add_done_callback(_on_done)
    return task


def install_loop_exception_handler() -> None:
    """Log asyncio callback/task failures to the error queue (and keep default stderr behavior)."""

    loop = asyncio.get_running_loop()

    def handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        loop.default_exception_handler(context)
        exc = context.get("exception")
        if exc is None:
            return
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            return
        db = _bound_db
        if db is None:
            return

        msg = str(context.get("message", ""))[:800] or repr(exc)
        safe_ctx: dict[str, Any] = {}
        for k, v in context.items():
            if k == "exception":
                continue
            try:
                safe_ctx[k] = repr(v)[:400]
            except Exception:
                safe_ctx[k] = "<unrepr>"

        async def _flush() -> None:
            try:
                await get_error_service(db).log_error(
                    feature="Asyncio: unhandled task/future error",
                    message=msg,
                    exception=exc if isinstance(exc, Exception) else None,
                    context=safe_ctx,
                )
            except Exception as e:
                logger.error("Failed to log asyncio error to queue: %s", e)

        try:
            loop.create_task(_flush())
        except RuntimeError:
            pass

    loop.set_exception_handler(handler)
