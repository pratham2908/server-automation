"""SMTP email delivery.

A thin, generic sender. It is deliberately graceful: when SMTP is not
configured (no host/from) or there is no recipient, the send is logged and
skipped rather than raised, so callers like the auto-scheduler keep running
without mail set up. The blocking smtplib work runs off the event loop.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

from app.config import Settings
from app.logger import get_logger

logger = get_logger(__name__)


def build_message(sender: str, recipient: str, subject: str, body: str) -> EmailMessage:
    """Build a plain-text email message. Pure — no network."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def _is_configured(settings: Settings) -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_FROM)


def _send_sync(settings: Settings, msg: EmailMessage) -> None:
    """Blocking SMTP send — run via ``asyncio.to_thread`` only."""
    # host/from presence is guaranteed by _is_configured before we get here.
    with smtplib.SMTP(str(settings.SMTP_HOST), settings.SMTP_PORT, timeout=30) as smtp:
        if settings.SMTP_USE_TLS:
            smtp.starttls()
        if settings.SMTP_USER and settings.SMTP_PASSWORD:
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        smtp.send_message(msg)


async def send_email(settings: Settings, recipient: str | None, subject: str, body: str) -> bool:
    """Send an email, returning whether it was actually sent.

    Returns ``False`` (without raising) when SMTP is unconfigured, when there is
    no recipient, or when the send fails — the caller treats mail as best-effort.
    """
    if not _is_configured(settings):
        logger.warning("SMTP not configured — email skipped. Subject: %s", subject)
        return False
    if not recipient:
        logger.warning("No recipient — email skipped. Subject: %s", subject)
        return False

    # SMTP_FROM is non-None here by _is_configured.
    msg = build_message(str(settings.SMTP_FROM), recipient, subject, body)
    try:
        await asyncio.to_thread(_send_sync, settings, msg)
        logger.info("Sent email '%s' to %s", subject, recipient)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        # Mail is best-effort; surface the failure in logs but never break the run.
        logger.error("Failed to send email '%s' to %s: %s", subject, recipient, exc)
        return False
