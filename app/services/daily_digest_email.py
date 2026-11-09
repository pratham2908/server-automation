"""The daily digest as an email. Pure: a digest dict in, subject / text / HTML out.

Shape of the input (see ``daily_digest.build_digest``)::

    {"date": "2026-11-08",
     "totals": {"channels", "followers_change", "views", "posts"},
     "channels": [{"name", "handle", "platform", "thumbnail_url", "followers", "followers_change",
                   "views", "posts": [{"title", "url"}], "top_posts": [{"title", "views", "url"}]}]}

Written for mail clients: tables, inline styles, no scripts, system fonts. A client that
refuses remote images shows the channel's initial on a coloured disc instead.
"""

from __future__ import annotations

from datetime import date
from html import escape
from typing import Any

from app.services.auto_scheduler_summary import SummaryEmail

_INK = "#12151c"
_MUTED = "#6a7183"
_LINE = "#e6e8ee"
_PAGE = "#eceef3"
_CARD = "#ffffff"
_TILE = "#f5f6f9"
# The header band and its two text colours.
_BAND = "#0e2a33"
_BAND_TEXT = "#f4efe6"
_BAND_MUTED = "#8fb0b8"
_ACCENT = "#e9a93b"
_UP = "#17803d"
_UP_BG = "#dcf5e4"
_DOWN = "#b42318"
_DOWN_BG = "#fde4e1"
_FLAT = "#6a7183"
_FLAT_BG = "#eceef3"
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"
_AVATARS = ("#0e7490", "#b45309", "#6d28d9", "#be123c", "#0f766e", "#4338ca")
_PLATFORMS = {"instagram": "Instagram", "youtube": "YouTube", "threads": "Threads"}


def compact(value: float | None) -> str:
    """12400 -> 12.4k; None -> a dash."""
    if value is None:
        return "—"
    n = abs(value)
    sign = "-" if value < 0 else ""
    if n >= 1_000_000:
        return f"{sign}{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{sign}{n / 1000:.1f}k"
    if n >= 1000:
        return f"{sign}{n:,.0f}"
    return f"{sign}{n:.0f}"


def signed(value: float | None) -> str:
    if value is None:
        return "—"
    return f"+{compact(value)}" if value > 0 else compact(value)


def long_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B')}"


def _tone(change: float | None) -> tuple[str, str]:
    if change is None or change == 0:
        return _FLAT, _FLAT_BG
    return (_UP, _UP_BG) if change > 0 else (_DOWN, _DOWN_BG)


def _chip(change: float | None) -> str:
    colour, background = _tone(change)
    return (
        f'<span style="display:inline-block;padding:3px 10px;border-radius:12px;background:{background};'
        f'color:{colour};font:700 12px/1.4 {_FONT};white-space:nowrap;">{escape(signed(change))}</span>'
    )


def _avatar(name: str, thumbnail: str | None) -> str:
    if thumbnail:
        return (
            f'<img src="{escape(thumbnail, quote=True)}" width="44" height="44" alt="{escape(name, quote=True)}" '
            f'style="width:44px;height:44px;border-radius:22px;object-fit:cover;display:block;" />'
        )
    colour = _AVATARS[sum(map(ord, name)) % len(_AVATARS)]
    initial = escape(name.strip()[:1].upper() or "?")
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td width="44" height="44" align="center" valign="middle" style="width:44px;height:44px;border-radius:22px;'
        f'background:{colour};color:#ffffff;font:700 18px/44px {_FONT};text-align:center;">{initial}</td></tr></table>'
    )


def _link(text: str, url: str | None, colour: str = _INK) -> str:
    label = escape(text if len(text) <= 70 else text[:67] + "…")
    if not url:
        return label
    return f'<a href="{escape(url, quote=True)}" style="color:{colour};text-decoration:none;">{label}</a>'


def _tile(label: str, value: str, colour: str = _INK) -> str:
    return (
        f'<td width="33%" valign="top" style="padding:0 4px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{_TILE};border-radius:10px;"><tr><td style="padding:11px 12px;">'
        f'<div style="font:600 10px/1.4 {_FONT};letter-spacing:.09em;text-transform:uppercase;color:{_MUTED};">'
        f"{escape(label)}</div>"
        f'<div style="font:700 21px/1.25 {_FONT};color:{colour};padding-top:2px;">{escape(value)}</div>'
        f"</td></tr></table></td>"
    )


def _band_stat(value: str, label: str) -> str:
    return (
        f'<td width="33%" valign="top">'
        f'<div style="font:700 28px/1.1 {_FONT};color:{_BAND_TEXT};">{escape(value)}</div>'
        f'<div style="font:600 10px/1.4 {_FONT};letter-spacing:.1em;text-transform:uppercase;color:{_BAND_MUTED};'
        f'padding-top:4px;">{escape(label)}</div></td>'
    )


def _top_posts(posts: list[dict[str, Any]]) -> str:
    if not posts:
        return ""
    rows = "".join(
        f'<tr><td width="22" valign="top" style="padding:6px 0;font:700 12px/1.5 {_FONT};color:{_ACCENT};">{i}</td>'
        f'<td style="padding:6px 8px 6px 0;font:500 14px/1.45 {_FONT};color:{_INK};">'
        f"{_link(p['title'], p.get('url'))}</td>"
        f'<td align="right" valign="top" style="padding:6px 0;font:700 13px/1.5 {_FONT};color:{_INK};white-space:nowrap;">'
        f'{escape(compact(p["views"]))}<span style="font-weight:500;color:{_MUTED};"> views</span></td></tr>'
        for i, p in enumerate(posts, 1)
    )
    return (
        f'<div style="font:600 10px/1.4 {_FONT};letter-spacing:.09em;text-transform:uppercase;color:{_MUTED};'
        f'padding:16px 0 2px;">Top posts of the day</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>'
    )


def _published(posts: list[dict[str, Any]]) -> str:
    if not posts:
        return ""
    rows = "".join(
        f'<div style="font:400 13px/1.5 {_FONT};color:{_MUTED};padding:2px 0;">'
        f"{_link(p['title'], p.get('url'), _MUTED)}</div>"
        for p in posts
    )
    return (
        f'<div style="font:600 10px/1.4 {_FONT};letter-spacing:.09em;text-transform:uppercase;color:{_MUTED};'
        f'padding:14px 0 4px;">Published</div>{rows}'
    )


def _channel_card(c: dict[str, Any]) -> str:
    name = str(c["name"])
    platform = _PLATFORMS.get(c["platform"], str(c["platform"]).title())
    sub = " · ".join(x for x in (str(c.get("handle") or ""), platform) if x)
    change = c.get("followers_change")
    colour, _ = _tone(change)
    return (
        f'<tr><td style="padding:0 20px 14px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border:1px solid {_LINE};border-radius:14px;"><tr><td style="padding:16px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td width="56" valign="middle">{_avatar(name, c.get("thumbnail_url"))}</td>'
        f'<td valign="middle"><div style="font:700 16px/1.3 {_FONT};color:{_INK};">{escape(name)}</div>'
        f'<div style="font:400 12px/1.5 {_FONT};color:{_MUTED};">{escape(sub)}</div></td>'
        f'<td align="right" valign="middle">{_chip(change)}</td></tr></table>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin-top:14px;"><tr>'
        f"{_tile('Followers', compact(c.get('followers')))}"
        f"{_tile('Gained', signed(change), colour)}"
        f"{_tile('Views', compact(c.get('views')))}"
        f"</tr></table>"
        f"{_top_posts(c.get('top_posts') or [])}{_published(c.get('posts') or [])}"
        f"</td></tr></table></td></tr>"
    )


def _best_day(channels: list[dict[str, Any]]) -> str:
    ranked = [c for c in channels if c.get("views")]
    if len(channels) < 2 or not ranked:
        return ""
    best = max(ranked, key=lambda c: c["views"])
    return (
        f'<tr><td style="padding:18px 20px 14px;">'
        f'<div style="font:400 14px/1.5 {_FONT};color:{_INK};">'
        f'<span style="color:{_ACCENT};font-weight:700;">Best day</span> '
        f"<strong>{escape(str(best['name']))}</strong> pulled in {escape(compact(best['views']))} views.</div></td></tr>"
    )


def _html(digest: dict[str, Any], app_url: str) -> str:
    totals = digest["totals"]
    channels = digest["channels"]
    n = totals["channels"]
    cards = "".join(_channel_card(c) for c in channels)
    return (
        f'<div style="margin:0;padding:24px 12px;background:{_PAGE};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td align="center">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;max-width:600px;background:{_CARD};border-radius:18px;overflow:hidden;">'
        f'<tr><td style="background:{_BAND};padding:26px 24px 24px;">'
        f'<div style="font:700 11px/1.4 {_FONT};letter-spacing:.16em;text-transform:uppercase;color:{_ACCENT};">'
        f"Daily summary</div>"
        f'<div style="font:700 25px/1.25 {_FONT};color:{_BAND_TEXT};padding:6px 0 22px;">'
        f"{escape(long_date(digest['date']))}</div>"
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f"{_band_stat(signed(totals['followers_change']), 'Followers')}"
        f"{_band_stat(compact(totals['views']), 'Views')}"
        f"{_band_stat(str(totals['posts']), 'Posts')}"
        f"</tr></table>"
        f'<div style="font:400 12px/1.5 {_FONT};color:{_BAND_MUTED};padding-top:18px;">'
        f"Across {n} active channel{'s' if n != 1 else ''}</div></td></tr>"
        f"{_best_day(channels)}"
        f'<tr><td style="padding-top:{0 if len(channels) > 1 else 18}px;"></td></tr>'
        f"{cards}"
        f'<tr><td style="padding:6px 24px 24px;border-top:1px solid {_LINE};">'
        f'<div style="font:400 12px/1.6 {_FONT};color:{_MUTED};padding-top:16px;">'
        f"Active means posted in the last 7 days. Followers are the day's last reading against the day before; "
        f"views are what all posts on the channel gained between midnight and midnight (IST).</div>"
        f'<div style="padding-top:12px;"><a href="{escape(app_url, quote=True)}" '
        f'style="font:700 13px/1.4 {_FONT};color:{_BAND};text-decoration:underline;">Open the growth charts</a>'
        f"</div></td></tr>"
        f"</table></td></tr></table></div>"
    )


def _text(digest: dict[str, Any], app_url: str) -> str:
    totals = digest["totals"]
    lines = [
        f"Daily summary - {long_date(digest['date'])}",
        f"{signed(totals['followers_change'])} followers, {compact(totals['views'])} views, "
        f"{totals['posts']} posts across {totals['channels']} active channels",
        "",
    ]
    for c in digest["channels"]:
        lines.append(f"{c['name']} ({_PLATFORMS.get(c['platform'], c['platform'])})")
        lines.append(
            f"  Followers {compact(c.get('followers'))} ({signed(c.get('followers_change'))}), "
            f"views {compact(c.get('views'))}"
        )
        for i, p in enumerate(c.get("top_posts") or [], 1):
            lines.append(f"  {i}. {p['title']} - {compact(p['views'])} views")
        for p in c.get("posts") or []:
            lines.append(f"  Published: {p['title']}")
        lines.append("")
    lines.append(f"Growth charts: {app_url}")
    return "\n".join(lines)


def format_digest_email(digest: dict[str, Any], app_url: str) -> SummaryEmail:
    totals = digest["totals"]
    when = date.fromisoformat(digest["date"])
    subject = (
        f"Daily summary · {when.strftime('%a')} {when.day} {when.strftime('%b')}: "
        f"{signed(totals['followers_change'])} followers, {compact(totals['views'])} views"
    )
    return SummaryEmail(subject=subject, text=_text(digest, app_url), html=_html(digest, app_url))
