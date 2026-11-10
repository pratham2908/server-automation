"""The "comments waiting to be pinned" email: a page button, and a direct link for every pending reel.

Pure formatting plus one sender. Same look as the other emails.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from typing import Any

from app.config import Settings
from app.logger import get_logger
from app.services.auto_scheduler_summary import SummaryEmail
from app.services.daily_digest_email import (
    _ACCENT,
    _BAND,
    _BAND_MUTED,
    _BAND_TEXT,
    _CARD,
    _FONT,
    _INK,
    _LINE,
    _MUTED,
    _PAGE,
    _TILE,
)
from app.services.email_service import resolve_owner_email, send_email
from app.services.pin_tracker import pins_overview
from app.timezone import assume_utc, now_ist

logger = get_logger(__name__)

PAGE_PATH = "/?hub=production&view=pins"
CONFIG_KEY = "pin_email"
# New reels needing a pin arrive through the day; one email per stretch beats one per reel.
MIN_GAP = timedelta(hours=3)
MAX_TITLE = 80


def _clip(text: str, limit: int) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    d = datetime.fromisoformat(iso)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}, {d.strftime('%H:%M')}"


def _button(url: str, label: str, *, primary: bool = True) -> str:
    colours = f"background:{_BAND};color:{_BAND_TEXT};" if primary else f"background:{_PAGE};color:{_INK};"
    return (
        f'<a href="{escape(url, quote=True)}" style="display:inline-block;padding:9px 16px;border-radius:9px;'
        f'{colours}font:700 13px/1.2 {_FONT};text-decoration:none;margin-right:8px;">{escape(label)}</a>'
    )


def _row(item: dict[str, Any]) -> str:
    links = ""
    if item.get("comment_url"):
        links += _button(item["comment_url"], "Open comment to pin")
    if item.get("reel_url"):
        links += _button(item["reel_url"], "Open reel", primary=False)
    comment = (
        f'<div style="margin-top:8px;padding:8px 12px;background:{_TILE};border-radius:9px;'
        f'font:400 13px/1.5 {_FONT};color:{_INK};">{escape(_clip(item["comment_text"], 160))}</div>'
        if item.get("comment_text")
        else ""
    )
    return (
        f'<tr><td style="padding:14px 0;border-top:1px solid {_LINE};">'
        f'<div style="font:600 14px/1.45 {_FONT};color:{_INK};">{escape(_clip(item["title"], MAX_TITLE))}</div>'
        f'<div style="font:400 12px/1.5 {_FONT};color:{_MUTED};padding-top:2px;">'
        f"Comment posted {escape(_when(item.get('posted_at')))}</div>"
        f'{comment}<div style="padding-top:10px;">{links}</div></td></tr>'
    )


def _channel(block: dict[str, Any]) -> str:
    n = len(block["pending"])
    sub = " · ".join(x for x in (str(block.get("handle") or ""), f"{n} to pin") if x)
    return (
        f'<tr><td style="padding:22px 24px 4px;">'
        f'<div style="font:700 17px/1.3 {_FONT};color:{_INK};">{escape(str(block["name"]))}</div>'
        f'<div style="font:400 12px/1.5 {_FONT};color:{_MUTED};">{escape(sub)}</div></td></tr>'
        f'<tr><td style="padding:0 24px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0">{"".join(_row(i) for i in block["pending"])}</table></td></tr>'
    )


def format_pins_email(overview: dict[str, Any], app_url: str) -> SummaryEmail:
    blocks = [b for b in overview["channels"] if b["pending"]]
    total = overview["pending_total"]
    page = app_url.rstrip("/") + PAGE_PATH
    html = (
        f'<div style="margin:0;padding:24px 12px;background:{_PAGE};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td align="center">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;max-width:600px;background:{_CARD};border-radius:18px;overflow:hidden;">'
        f'<tr><td style="background:{_BAND};padding:26px 24px;">'
        f'<div style="font:700 11px/1.4 {_FONT};letter-spacing:.16em;text-transform:uppercase;color:{_ACCENT};">'
        f"Pinned comments</div>"
        f'<div style="font:700 25px/1.25 {_FONT};color:{_BAND_TEXT};padding:6px 0 4px;">'
        f"{total} waiting to be pinned</div>"
        f'<div style="font:400 13px/1.5 {_FONT};color:{_BAND_MUTED};padding-bottom:16px;">'
        f"Across {len(blocks)} channel{'s' if len(blocks) != 1 else ''}</div>"
        f'<a href="{escape(page, quote=True)}" style="display:inline-block;padding:11px 20px;border-radius:10px;'
        f'background:{_ACCENT};color:#1a1305;font:700 14px/1.2 {_FONT};text-decoration:none;">'
        f"Open the pinning page</a></td></tr>"
        f'<tr><td style="padding:16px 24px 0;font:400 13px/1.6 {_FONT};color:{_MUTED};">'
        f"Tap <strong>Open comment to pin</strong> on your phone: it lands on our comment in Instagram. "
        f"Press and hold it, choose <strong>Pin</strong>, then tick the reel on the pinning page.</td></tr>"
        f"{''.join(_channel(b) for b in blocks)}"
        f'<tr><td style="padding:18px 24px 24px;font:400 12px/1.6 {_FONT};color:{_MUTED};">'
        f"Instagram does not tell us whether a comment is pinned, so the page is where you mark it.</td></tr>"
        f"</table></td></tr></table></div>"
    )
    lines = [f"{total} comments waiting to be pinned", f"Pinning page: {page}", ""]
    for b in blocks:
        lines.append(f"{b['name']} ({len(b['pending'])} to pin)")
        for i in b["pending"]:
            lines.append(f"  {_clip(i['title'], MAX_TITLE)}")
            lines.append(f"    Comment: {i.get('comment_url') or '(no link)'}")
            lines.append(f"    Reel: {i.get('reel_url') or '(no link)'}")
        lines.append("")
    return SummaryEmail(subject=f"{total} comments waiting to be pinned", text="\n".join(lines), html=html)


def unnotified(overview: dict[str, Any], notified: set[str]) -> list[str]:
    return [i["video_id"] for b in overview["channels"] for i in b["pending"] if i["video_id"] not in notified]


async def send_pending_pins_email(db: Any, manager: Any, settings: Settings, *, force: bool = False) -> str:
    """Email every pending reel when something new is waiting (or ``force``). Returns "sent", "nothing new", "too soon", "not sent"."""
    overview = await pins_overview(db, manager)
    if overview["pending_total"] == 0:
        return "nothing new"
    state = await db.config.find_one({"key": CONFIG_KEY}) or {}
    notified = set(state.get("notified_ids") or [])
    if not force and not unnotified(overview, notified):
        return "nothing new"
    last = state.get("last_sent_at")
    now = now_ist()
    if not force and last is not None and now - assume_utc(last) < MIN_GAP:
        return "too soon"
    email = format_pins_email(overview, settings.ANALYZER_PUBLIC_URL)
    if not await send_email(settings, await resolve_owner_email(db, settings), email.subject, email.text, email.html):
        return "not sent"
    ids = [i["video_id"] for b in overview["channels"] for i in b["pending"]]
    await db.config.update_one({"key": CONFIG_KEY}, {"$set": {"last_sent_at": now, "notified_ids": ids}}, upsert=True)
    return "sent"
