"""Background loop: re-measure R2 storage once a day (see ``storage_usage``)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.logger import get_logger
from app.services.error_reporting import report_error
from app.services.storage_usage import CHECK_INTERVAL_SECONDS, CONFIG_KEY, refresh_storage_usage

logger = get_logger(__name__)


async def _seconds_until_due(db: Any) -> float:
    """Wait out the rest of the day since the last reading, so restarts do not re-list the bucket."""

    doc = await db.config.find_one({"key": CONFIG_KEY}) or {}
    checked_at = doc.get("checked_at")
    if not checked_at:
        return 60.0
    age = (datetime.now(timezone.utc) - checked_at.replace(tzinfo=timezone.utc)).total_seconds()
    return max(60.0, CHECK_INTERVAL_SECONDS - age)


async def run_storage_usage_cron(db: Any, r2: Any) -> None:
    logger.info("Storage usage cron started (daily)")
    await asyncio.sleep(await _seconds_until_due(db))
    while True:
        try:
            await refresh_storage_usage(db, r2, get_settings())
        except Exception as exc:
            logger.error("Storage usage check failed: %s", exc)
            await report_error(feature="Storage usage cron", message=f"Storage check failed: {exc!s}", exception=exc)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
