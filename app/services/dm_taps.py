"""Taps on our buttons, chips, ice breakers and menu items, read out of Meta's webhook. Pure.

A tap reaches us one of two ways:

- a reply button, an ice breaker or a menu item arrives as a ``postback`` (``messaging_postbacks``);
- a quick-reply chip arrives as an ordinary ``message`` carrying ``quick_reply.payload`` — the chip's
  title is also the text of their message, so the inbox already keeps it as one.

Only the payload says which reply to send; see ``dm_content.reply_payload``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

TapKind = Literal["postback", "quick_reply"]


class DmTap(BaseModel):
    account_id: str
    sender_id: str
    # The postback's or message's id: Meta can deliver one tap twice, and this is what dedupes it.
    tap_id: str
    payload: str
    title: str = ""
    kind: TapKind
    at: datetime


def _stamp(item: Mapping[str, Any], entry: Mapping[str, Any]) -> datetime | None:
    stamp = item.get("timestamp") or entry.get("time")
    if not isinstance(stamp, int | float) or stamp <= 0:
        return None
    return datetime.fromtimestamp(stamp / 1000 if stamp > 1e11 else stamp, tz=UTC)


def _tap(item: Mapping[str, Any]) -> tuple[TapKind, Mapping[str, Any], str] | None:
    """(kind, the object carrying the payload, its title) for a tap, or None for anything else."""
    postback = item.get("postback")
    if isinstance(postback, Mapping) and postback.get("payload"):
        return "postback", postback, str(postback.get("title") or "")
    message = item.get("message")
    if isinstance(message, Mapping) and not message.get("is_echo"):
        chip = message.get("quick_reply")
        if isinstance(chip, Mapping) and chip.get("payload"):
            return "quick_reply", {**chip, "mid": message.get("mid")}, str(message.get("text") or "")
    return None


def parse_taps(payload: Mapping[str, Any]) -> list[DmTap]:
    """Every tap in one webhook delivery, in order. Echoes of our own messages are never taps."""
    if payload.get("object") != "instagram":
        return []
    taps: list[DmTap] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, Mapping):
            continue
        for item in entry.get("messaging") or []:
            if not isinstance(item, Mapping):
                continue
            found = _tap(item)
            at = _stamp(item, entry)
            sender = str((item.get("sender") or {}).get("id") or "")
            if found is None or at is None or not sender:
                continue
            kind, carrier, title = found
            tap_payload = str(carrier.get("payload"))
            # A postback from an ice breaker may come without a mid; who, what and when is still one tap.
            tap_id = str(carrier.get("mid") or f"{sender}:{tap_payload}:{int(at.timestamp() * 1000)}")
            taps.append(
                DmTap(
                    account_id=str(entry.get("id") or ""),
                    sender_id=sender,
                    tap_id=tap_id,
                    payload=tap_payload,
                    title=title,
                    kind=kind,
                    at=at,
                )
            )
    return taps
