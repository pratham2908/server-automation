from __future__ import annotations

import logging
import traceback
import uuid
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.timezone import now_ist

logger = logging.getLogger(__name__)


class ErrorService:
    """Service to handle logging errors to the database."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db

    async def log_error(
        self,
        feature: str,
        message: str,
        exception: Exception | None = None,
        context: dict[str, Any] | None = None,
    ):
        """
        Log an error to the database.

        Args:
            feature: The name of the feature where the error occurred.
            message: A human-readable error message.
            exception: The exception object (if any) to extract stack trace.
            context: Additional metadata about the error.
        """
        try:
            stack_trace = None
            if exception:
                stack_trace = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))

            now = now_ist()
            # Group errors by feature, message, and unresolved status
            await self.db.errors.update_one(
                {"feature": feature, "message": message, "resolved": False},
                {
                    "$inc": {"count": 1},
                    "$set": {
                        "last_occurred_at": now,
                        "stack_trace": stack_trace,
                        "context": context or {},
                    },
                    "$setOnInsert": {
                        "_id": str(uuid.uuid4()),
                        "timestamp": now,
                        "resolved": False,
                    },
                },
                upsert=True,
            )
            logger.info(f"Logged/Updated error for feature '{feature}': {message}")
        except Exception as e:
            # Fallback to standard logging if DB logging fails
            logger.error(f"Failed to log error to DB: {e}")
            logger.error(f"Original error ({feature}): {message}")


# Singleton-like access if needed, though usually injected via FastAPI dependencies
_error_service: ErrorService | None = None


def get_error_service(db: AsyncIOMotorDatabase) -> ErrorService:
    global _error_service
    if _error_service is None or _error_service.db != db:
        _error_service = ErrorService(db)
    return _error_service
