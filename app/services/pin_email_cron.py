"""Background loop: email the pending pins whenever new ones have piled up (see ``pin_email``)."""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import get_settings
from app.logger import get_logger
from app.services.error_reporting import report_error
from app.services.pin_email import send_pending_pins_email

logger = get_logger(__name__)

TICK_SECONDS = 600


async def run_pin_email_cron(db: Any, manager: Any) -> None:
    logger.info("Pin email cron started (every %ds)", TICK_SECONDS)
    await asyncio.sleep(120)
    while True:
        try:
            await send_pending_pins_email(db, manager, get_settings())
        except Exception as exc:
            logger.error("Pin email failed: %s", exc)
            await report_error(feature="Pin email cron", message=f"Pin email failed: {exc!s}", exception=exc)
        await asyncio.sleep(TICK_SECONDS)
