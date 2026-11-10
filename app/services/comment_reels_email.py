"""The "reels with comments" email: one row per reel, each with a button that opens it in Instagram.

Pure: a ``build_comment_reels`` dict in, subject / text / HTML out. Same look as the daily digest.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

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
    compact,
)

MAX_TITLE = 90
MAX_SAMPLE = 140


def _clip(text: str, limit: int) -> str:
    first = " ".join(text.split())
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def _posted(iso: str | None) -> str:
    if not iso:
        return ""
    d = datetime.fromisoformat(iso)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _button(url: str | None) -> str:
    if not url:
        return ""
    return (
        f'<a href="{escape(url, quote=True)}" style="display:inline-block;padding:9px 16px;border-radius:9px;'
        f'background:{_BAND};color:{_BAND_TEXT};font:700 13px/1.2 {_FONT};text-decoration:none;">Open reel</a>'
    )


def _sample(s: dict[str, Any]) -> str:
    body = f"<strong>@{escape(s['username'])}</strong> {escape(_clip(s['text'], MAX_SAMPLE))}"
    link = s.get("link")
    if link:
        body = f'<a href="{escape(link, quote=True)}" style="color:{_INK};text-decoration:none;">{body}</a>'
    return f'<div style="font:400 13px/1.5 {_FONT};color:{_INK};padding:3px 0;">{body}</div>'


def _reel(r: dict[str, Any]) -> str:
    meta = " · ".join(
        x
        for x in (
            _posted(r.get("published_at")),
            f"{compact(r['views'])} views" if r.get("views") is not None else "",
        )
        if x
    )
    samples = "".join(_sample(s) for s in r["samples"])
    samples_block = (
        f'<div style="margin-top:10px;padding:8px 12px;background:{_TILE};border-radius:9px;">{samples}</div>'
        if samples
        else ""
    )
    n = r["comment_count"]
    return (
        f'<tr><td style="padding:14px 0;border-top:1px solid {_LINE};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td valign="top"><div style="font:600 14px/1.45 {_FONT};color:{_INK};">{escape(_clip(r["title"], MAX_TITLE))}</div>'
        f'<div style="font:400 12px/1.5 {_FONT};color:{_MUTED};padding-top:2px;">{escape(meta)}</div></td>'
        f'<td width="1" align="right" valign="top" style="padding-left:12px;white-space:nowrap;">'
        f'<span style="display:inline-block;padding:3px 10px;border-radius:12px;background:{_PAGE};'
        f'color:{_INK};font:700 12px/1.4 {_FONT};">{n} comment{"s" if n != 1 else ""}</span></td></tr></table>'
        f"{samples_block}"
        f'<div style="padding-top:10px;">{_button(r.get("permalink"))}</div></td></tr>'
    )


def _channel(c: dict[str, Any]) -> str:
    sub = " · ".join(x for x in (str(c.get("handle") or ""), f"{len(c['reels'])} reels, {c['comments']} comments") if x)
    return (
        f'<tr><td style="padding:22px 24px 4px;">'
        f'<div style="font:700 17px/1.3 {_FONT};color:{_INK};">{escape(str(c["name"]))}</div>'
        f'<div style="font:400 12px/1.5 {_FONT};color:{_MUTED};">{escape(sub)}</div></td></tr>'
        f'<tr><td style="padding:0 24px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0">{"".join(_reel(r) for r in c["reels"])}</table></td></tr>'
    )


def _html(data: dict[str, Any]) -> str:
    totals = data["totals"]
    return (
        f'<div style="margin:0;padding:24px 12px;background:{_PAGE};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td align="center">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;max-width:600px;background:{_CARD};border-radius:18px;overflow:hidden;">'
        f'<tr><td style="background:{_BAND};padding:26px 24px;">'
        f'<div style="font:700 11px/1.4 {_FONT};letter-spacing:.16em;text-transform:uppercase;color:{_ACCENT};">'
        f"Reels with comments</div>"
        f'<div style="font:700 25px/1.25 {_FONT};color:{_BAND_TEXT};padding:6px 0 4px;">'
        f"{totals['reels']} reels, {totals['comments']:,} comments</div>"
        f'<div style="font:400 13px/1.5 {_FONT};color:{_BAND_MUTED};">Comments from the last {data["days"]} days</div>'
        f"</td></tr>"
        f'<tr><td style="padding:16px 24px 0;font:400 13px/1.6 {_FONT};color:{_MUTED};">'
        f"On your phone, <strong>Open reel</strong> lands in Instagram. Tap a comment below to jump straight to it, "
        f"then press and hold it and choose <strong>Pin</strong>.</td></tr>"
        f"{''.join(_channel(c) for c in data['channels'])}"
        f'<tr><td style="padding:18px 24px 24px;font:400 12px/1.6 {_FONT};color:{_MUTED};">'
        f"Other people's comments only. The three longest are shown for each reel.</td></tr>"
        f"</table></td></tr></table></div>"
    )


def _text(data: dict[str, Any]) -> str:
    lines = [f"Reels with comments - last {data['days']} days", ""]
    for c in data["channels"]:
        lines.append(f"{c['name']} ({len(c['reels'])} reels, {c['comments']} comments)")
        for r in c["reels"]:
            lines.append(f"  {_clip(r['title'], MAX_TITLE)} - {r['comment_count']} comments")
            lines.append(f"    {r.get('permalink') or '(no link)'}")
            for s in r["samples"]:
                lines.append(f"    @{s['username']}: {_clip(s['text'], MAX_SAMPLE)}")
                if s.get("link"):
                    lines.append(f"      {s['link']}")
        lines.append("")
    return "\n".join(lines)


def format_comment_reels_email(data: dict[str, Any]) -> SummaryEmail:
    totals = data["totals"]
    subject = f"{totals['reels']} reels with comments, last {data['days']} days"
    return SummaryEmail(subject=subject, text=_text(data), html=_html(data))
