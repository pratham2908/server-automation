"""Daily check of how much R2 storage the app holds, with an email when it gets large.

Listing the whole bucket is a few thousand objects, so it is done once a day and
the answer cached in ``config`` (``key: "storage_usage"``). The UI banner and the
API read that cache and never list the bucket themselves.

The alert is edge-triggered: one email when usage crosses the threshold, then a
reminder every ``REMINDER_DAYS`` while it stays over, and the state re-arms as soon
as usage drops back under (a purge refreshes the cache straight away).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from app.config import Settings
from app.logger import get_logger
from app.services.email_service import resolve_owner_email, send_email

logger = get_logger(__name__)

CONFIG_KEY = "storage_usage"
# Binary gigabytes, the same unit the Storage tab shows.
THRESHOLD_BYTES = int(7.5 * 1024**3)
REMINDER_DAYS = 7
CHECK_INTERVAL_SECONDS = 24 * 3600


def _gb(n: int) -> str:
    return f"{n / 1024**3:.2f} GB"


def summarize(objects: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
    """Total bytes and bytes per channel (the key's first path segment)."""

    by_channel: dict[str, int] = defaultdict(int)
    total = 0
    for obj in objects:
        size = int(obj.get("size", 0))
        total += size
        by_channel[str(obj.get("key", "")).split("/", 1)[0]] += size
    return total, dict(by_channel)


def should_alert(last_alert_at: datetime | None, now: datetime, is_over: bool) -> bool:
    """Email on crossing the threshold (no alert on record), then every REMINDER_DAYS while still over."""

    if not is_over:
        return False
    if last_alert_at is None:
        return True
    if last_alert_at.tzinfo is None:
        last_alert_at = last_alert_at.replace(tzinfo=timezone.utc)
    return now - last_alert_at >= timedelta(days=REMINDER_DAYS)


_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"


def build_alert_email(total: int, by_channel: dict[str, int], public_url: str) -> tuple[str, str, str]:
    """(subject, text, html) for the over-threshold email."""

    top = sorted(by_channel.items(), key=lambda kv: -kv[1])[:5]
    over_by = total - THRESHOLD_BYTES
    subject = f"Storage is at {_gb(total)}, {_gb(over_by)} over the limit"
    lines = [
        f"R2 storage holds {_gb(total)}, {_gb(over_by)} above the {_gb(THRESHOLD_BYTES)} limit.",
        "",
        "Largest channels:",
        *[f"  {name or '(root)'}: {_gb(size)}" for name, size in top],
        "",
        f"Free space from the Storage tab: {public_url}",
        f"You will be reminded every {REMINDER_DAYS} days while it stays above the limit.",
    ]
    biggest = top[0][1] if top else 1
    rows = "".join(
        f'<tr><td style="padding:7px 12px 7px 0;font:500 14px/1.4 {_FONT};color:#12151c;white-space:nowrap;">'
        f"{escape(name or '(root)')}</td>"
        f'<td width="100%" style="padding:7px 12px 7px 0;">'
        f'<div style="height:8px;border-radius:4px;background:#eceef3;">'
        f'<div style="height:8px;border-radius:4px;background:#e9a93b;width:{max(3, round(size / biggest * 100))}%;"></div>'
        f"</div></td>"
        f'<td align="right" style="padding:7px 0;font:700 13px/1.4 {_FONT};color:#12151c;white-space:nowrap;">'
        f"{_gb(size)}</td></tr>"
        for name, size in top
    )
    used = min(100, round(total / THRESHOLD_BYTES * 100))
    html = (
        f'<div style="margin:0;padding:24px 12px;background:#eceef3;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td align="center">'
        f'<table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;max-width:560px;background:#ffffff;border-radius:18px;overflow:hidden;">'
        f'<tr><td style="background:#0e2a33;padding:26px 24px;">'
        f'<div style="font:700 11px/1.4 {_FONT};letter-spacing:.16em;text-transform:uppercase;color:#e9a93b;">'
        f"Storage warning</div>"
        f'<div style="font:700 34px/1.15 {_FONT};color:#f4efe6;padding:8px 0 4px;">{_gb(total)}</div>'
        f'<div style="font:400 14px/1.5 {_FONT};color:#8fb0b8;">{_gb(over_by)} over the {_gb(THRESHOLD_BYTES)} limit</div>'
        f'<div style="height:8px;border-radius:4px;background:#1d4551;margin-top:18px;">'
        f'<div style="height:8px;border-radius:4px;background:#e9a93b;width:{used}%;"></div></div>'
        f"</td></tr>"
        f'<tr><td style="padding:22px 24px 6px;">'
        f'<div style="font:600 10px/1.4 {_FONT};letter-spacing:.09em;text-transform:uppercase;color:#6a7183;'
        f'padding-bottom:6px;">Largest channels</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>'
        f"</td></tr>"
        f'<tr><td style="padding:18px 24px 8px;">'
        f'<a href="{escape(public_url, quote=True)}" style="display:inline-block;padding:12px 22px;border-radius:10px;'
        f'background:#0e2a33;color:#f4efe6;font:700 14px/1.2 {_FONT};text-decoration:none;">Open Storage and purge</a>'
        f"</td></tr>"
        f'<tr><td style="padding:10px 24px 24px;font:400 12px/1.6 {_FONT};color:#6a7183;">'
        f"Old published and archived videos can be purged. You will be reminded every {REMINDER_DAYS} days "
        f"while storage stays over the limit.</td></tr>"
        f"</table></td></tr></table></div>"
    )
    return subject, "\n".join(lines), html


async def get_cached_usage(db: Any) -> dict[str, Any]:
    """The last stored reading, or an empty one before the first check has run."""

    doc = await db.config.find_one({"key": CONFIG_KEY}) or {}
    checked_at = doc.get("checked_at")
    return {
        "total_bytes": doc.get("total_bytes"),
        "object_count": doc.get("object_count"),
        "threshold_bytes": THRESHOLD_BYTES,
        "over_threshold": bool(doc.get("over_threshold", False)),
        "checked_at": checked_at.replace(tzinfo=timezone.utc).isoformat() if checked_at else None,
        "by_channel": doc.get("by_channel", {}),
    }


async def refresh_storage_usage(db: Any, r2: Any, settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    """List the bucket, store the totals, and send the alert if one is due."""

    now = now or datetime.now(timezone.utc)
    objects = await asyncio.to_thread(r2.list_objects_with_prefix, "")
    total, by_channel = summarize(objects)
    is_over = total > THRESHOLD_BYTES

    previous = await db.config.find_one({"key": CONFIG_KEY}) or {}
    # Dropping back under clears the record, which re-arms the alert for the next crossing.
    last_alert_at = previous.get("last_alert_at") if is_over else None

    if should_alert(last_alert_at, now, is_over):
        subject, text, html = build_alert_email(total, by_channel, settings.ANALYZER_PUBLIC_URL)
        recipient = await resolve_owner_email(db, settings)
        if await send_email(settings, recipient, subject, text, html):
            last_alert_at = now
        else:
            # Not sent (SMTP down or unset): leave the record as it was so tomorrow's check tries again.
            logger.warning("Storage alert email not sent; will retry at the next check")

    await db.config.update_one(
        {"key": CONFIG_KEY},
        {
            "$set": {
                "total_bytes": total,
                "object_count": len(objects),
                "over_threshold": is_over,
                "checked_at": now,
                "by_channel": by_channel,
                "last_alert_at": last_alert_at,
            }
        },
        upsert=True,
    )
    logger.info("Storage usage %s across %d objects (over limit: %s)", _gb(total), len(objects), is_over)
    return await get_cached_usage(db)
