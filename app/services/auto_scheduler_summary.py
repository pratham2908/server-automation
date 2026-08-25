"""Pure formatting of the daily auto-scheduler run into an email.

Kept separate from the cron worker so it imports nothing with side effects and
stays trivially testable. The summary dict is assembled by the worker; shape::

    {
      "date": "2026-08-24",
      "scheduled": [{"channel_id", "channel_name"?, "channel_thumbnail"?,
                     "slot", "video_title"?, "source"?}],
      "skipped":   [{"channel_id", "channel_name"?, "channel_thumbnail"?,
                     "slot"?, "reason"}],
    }

Two bodies come out: HTML for reading, and plain text for clients that refuse
it. Both carry the same facts, so nothing is only visible in one of them.

Written for mail clients, not browsers — tables rather than flexbox, inline
styles rather than a stylesheet, and no JavaScript. Outlook will square off the
rounded corners and that is the whole of the degradation.
"""

from __future__ import annotations

from html import escape
from typing import Any, NamedTuple

# A digest is scanned, not read: enough colour to separate an outcome from a
# problem, and nothing competing with it.
_INK = "#16181d"
_MUTED = "#6b7280"
_LINE = "#e5e7eb"
_PAGE = "#f4f5f7"
_CARD = "#ffffff"
_GOOD = "#15803d"
_GOOD_BG = "#dcfce7"
_WARN = "#b45309"
_WARN_BG = "#fef3c7"

_FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"
)

# Stand-in avatar colours, picked by name so a channel keeps the same one.
_AVATAR_COLOURS = ("#4f46e5", "#0891b2", "#c2410c", "#7c3aed", "#0f766e", "#be123c")


class SummaryEmail(NamedTuple):
    subject: str
    text: str
    html: str


def _label(entry: dict[str, Any]) -> str:
    """Prefer a human channel name, falling back to the id."""
    return str(entry.get("channel_name") or entry.get("channel_id") or "unknown")


def _avatar_colour(name: str) -> str:
    return _AVATAR_COLOURS[sum(map(ord, name)) % len(_AVATAR_COLOURS)]


def _avatar(entry: dict[str, Any]) -> str:
    """The channel's picture, or its initial on a coloured disc.

    A real picture can still fail to load — Instagram's CDN links expire, and
    most clients block remote images until the reader allows them — so the
    ``alt`` is the channel name rather than decoration, and a channel with no
    picture at all gets the disc instead of a broken-image icon.
    """
    name = _label(entry)
    thumbnail = entry.get("channel_thumbnail")
    if thumbnail:
        return (
            f'<img src="{escape(str(thumbnail), quote=True)}" width="40" height="40" '
            f'alt="{escape(name, quote=True)}" '
            'style="width:40px;height:40px;border-radius:20px;object-fit:cover;display:block;'
            f'border:1px solid {_LINE};" />'
        )
    initial = escape(name.strip()[:1].upper() or "?")
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td width="40" height="40" align="center" valign="middle" '
        f'style="width:40px;height:40px;border-radius:20px;background:{_avatar_colour(name)};'
        f'color:#ffffff;font:700 16px/40px {_FONT};text-align:center;">{initial}</td>'
        f"</tr></table>"
    )


def _pill(text: str, colour: str, background: str) -> str:
    return (
        f'<span style="display:inline-block;padding:3px 9px;border-radius:11px;'
        f'background:{background};color:{colour};font:700 11px/1.4 {_FONT};'
        f'letter-spacing:.04em;white-space:nowrap;">{escape(text)}</span>'
    )


def _row(entry: dict[str, Any], *, scheduled: bool) -> str:
    """One channel's outcome: who, when, and what — in that order."""
    name = escape(_label(entry))
    slot = escape(str(entry.get("slot") or ""))

    if scheduled:
        detail = escape(str(entry.get("video_title") or "Untitled"))
        detail_style = f"font:600 14px/1.45 {_FONT};color:{_INK};"
    else:
        detail = escape(str(entry.get("reason") or "skipped"))
        detail_style = f"font:600 13px/1.45 {_FONT};color:{_WARN};"

    source = entry.get("source")
    source_line = (
        f'<div style="font:400 12px/1.5 {_FONT};color:{_MUTED};padding-top:2px;">'
        f"via {escape(str(source))}</div>"
        if source
        else ""
    )
    slot_cell = (
        f'<td align="right" valign="top" style="padding:14px 18px 14px 8px;white-space:nowrap;">'
        f"{_pill(slot, _GOOD if scheduled else _WARN, _GOOD_BG if scheduled else _WARN_BG)}</td>"
        if slot
        else '<td style="padding:14px 18px 14px 8px;"></td>'
    )

    return (
        f'<tr><td width="58" valign="top" style="padding:14px 0 14px 18px;">{_avatar(entry)}</td>'
        f'<td valign="top" style="padding:14px 0;">'
        f'<div style="font:700 14px/1.4 {_FONT};color:{_INK};">{name}</div>'
        f'<div style="{detail_style}padding-top:3px;">{detail}</div>'
        f"{source_line}</td>"
        f"{slot_cell}</tr>"
    )


def _section(title: str, entries: list[dict[str, Any]], *, scheduled: bool, empty: str) -> str:
    heading = (
        f'<tr><td colspan="3" style="padding:20px 18px 6px;">'
        f'<div style="font:700 11px/1.4 {_FONT};letter-spacing:.09em;text-transform:uppercase;'
        f'color:{_MUTED};">{escape(title)} ({len(entries)})</div></td></tr>'
    )
    if not entries:
        return heading + (
            f'<tr><td colspan="3" style="padding:6px 18px 16px;font:400 13px/1.5 {_FONT};'
            f'color:{_MUTED};">{escape(empty)}</td></tr>'
        )
    rows = []
    for index, entry in enumerate(entries):
        divider = "" if index == 0 else f"border-top:1px solid {_LINE};"
        rows.append(f'<tr><td colspan="3" style="{divider}font-size:0;line-height:0;">&nbsp;</td></tr>')
        rows.append(_row(entry, scheduled=scheduled))
    return heading + "".join(rows)


def _stat(value: int, label: str, colour: str) -> str:
    return (
        f'<td width="50%" align="center" style="padding:14px 8px;">'
        f'<div style="font:700 26px/1.1 {_FONT};color:{colour};">{value}</div>'
        f'<div style="font:600 11px/1.4 {_FONT};letter-spacing:.08em;text-transform:uppercase;'
        f'color:{_MUTED};padding-top:3px;">{escape(label)}</div></td>'
    )


def _format_html(summary: dict[str, Any]) -> str:
    day = escape(str(summary.get("date", "")))
    scheduled = summary.get("scheduled", [])
    skipped = summary.get("skipped", [])

    return (
        f'<div style="margin:0;padding:24px 12px;background:{_PAGE};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td align="center">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;max-width:600px;background:{_CARD};border:1px solid {_LINE};'
        f'border-radius:14px;overflow:hidden;">'
        # Header
        f'<tr><td style="padding:20px 18px 0;">'
        f'<div style="font:700 17px/1.3 {_FONT};color:{_INK};">Auto-scheduler</div>'
        f'<div style="font:400 13px/1.5 {_FONT};color:{_MUTED};padding-top:2px;">{day}</div>'
        f"</td></tr>"
        # Counts, so the whole day reads at a glance
        f'<tr><td style="padding:14px 18px 0;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{_PAGE};border-radius:10px;"><tr>'
        f"{_stat(len(scheduled), 'Scheduled', _GOOD)}"
        f"{_stat(len(skipped), 'Skipped', _WARN if skipped else _MUTED)}"
        f"</tr></table></td></tr>"
        # Detail
        f'<tr><td style="padding:0 0 6px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        + _section("Scheduled", scheduled, scheduled=True, empty="Nothing was scheduled today.")
        + _section("Skipped", skipped, scheduled=False, empty="Nothing was skipped.")
        + f"</table></td></tr>"
        # Footer
        f'<tr><td style="padding:14px 18px 18px;border-top:1px solid {_LINE};">'
        f'<div style="font:400 12px/1.5 {_FONT};color:{_MUTED};">'
        f"Sent once a day, after every channel's slots have run.</div>"
        f"</td></tr>"
        f"</table></td></tr></table></div>"
    )


def _format_text(summary: dict[str, Any]) -> str:
    """Plain-text twin, for clients that will not render HTML."""
    day = summary.get("date", "")
    scheduled = summary.get("scheduled", [])
    skipped = summary.get("skipped", [])

    lines: list[str] = [f"Auto-scheduler run for {day}", ""]

    lines.append(f"Scheduled ({len(scheduled)}):")
    if scheduled:
        for entry in scheduled:
            source = f" via {entry['source']}" if entry.get("source") else ""
            title = entry.get("video_title") or "Untitled"
            lines.append(f"  - {_label(entry)} @ {entry.get('slot', '')} — {title}{source}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"Skipped ({len(skipped)}):")
    if skipped:
        for entry in skipped:
            slot = f" @ {entry['slot']}" if entry.get("slot") else ""
            lines.append(f"  - {_label(entry)}{slot} — {entry.get('reason', '')}")
    else:
        lines.append("  (none)")

    return "\n".join(lines)


def format_summary_email(summary: dict[str, Any]) -> SummaryEmail:
    """Return the subject and both bodies for a run summary."""
    day = summary.get("date", "")
    scheduled = summary.get("scheduled", [])
    skipped = summary.get("skipped", [])
    subject = f"Auto-scheduler: {len(scheduled)} scheduled, {len(skipped)} skipped ({day})"
    return SummaryEmail(subject=subject, text=_format_text(summary), html=_format_html(summary))
