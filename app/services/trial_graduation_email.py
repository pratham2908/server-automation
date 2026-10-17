"""The email that says a trial reel cleared its bar. Pure: text in, text out.

Built for a phone: the reader taps the button, lands on the reel in Instagram, and
shares it to followers. So the layout is one narrow column, the button comes before
the numbers, and everything is table-and-inline-style markup because mail clients
ignore stylesheets and most of modern CSS.
"""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
from typing import NamedTuple

from app.services.trial_graduation import METRIC_LABELS, OPERATOR_WORDS, ConditionResult, GroupResult

# Mail has no theme switching to lean on, so one palette that reads on a light or an
# auto-darkened client.
_INK = "#0f172a"
_MUTED = "#64748b"
_LINE = "#e2e8f0"
_PAGE = "#f1f5f9"
_CARD = "#ffffff"
_ACCENT = "#2563eb"
_GOOD = "#15803d"
_GOOD_BG = "#dcfce7"
_BAD = "#b91c1c"
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

# The numbers worth a glance, in reading order. Only those Instagram reported are shown.
_HEADLINE: tuple[tuple[str, str], ...] = (
    ("views", "Views"),
    ("reach", "Reached"),
    ("saves", "Saves"),
    ("shares", "Shares"),
    ("skip_rate", "Skip rate"),
    ("avg_view_duration_seconds", "Avg watch"),
)

_MAX_TITLE = 110
# Inbox lists and phone notifications cut a subject off early, so keep it short.
_MAX_SUBJECT_TITLE = 60


class TrialEmail(NamedTuple):
    subject: str
    text: str
    html: str


def _number(value: float | None) -> str:
    if value is None:
        return "not reported"
    return f"{int(value):,}" if float(value).is_integer() else f"{value:,.1f}"


def _stat(key: str, value: float) -> str:
    """A headline number with its unit: skip rate is a percentage, watch time is seconds."""
    shown = _number(value)
    if key == "skip_rate":
        return f"{shown}%"
    if key == "avg_view_duration_seconds":
        return f"{shown}s"
    return shown


def _short(title: str, limit: int = _MAX_TITLE) -> str:
    # The "title" of an Instagram reel is its caption, which can run to paragraphs.
    one_line = " ".join(title.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1].rstrip() + "…"


def _hours(hours: float) -> str:
    return f"{_number(hours)} hour{'' if hours == 1 else 's'}"


def _line(r: ConditionResult) -> str:
    label = METRIC_LABELS.get(r.metric, r.metric)
    return f"{label}: {_number(r.actual)} (needed {OPERATOR_WORDS[r.op]} {_number(r.value)})"


def _group_heading(index: int, group: GroupResult, total: int) -> str:
    if total == 1:
        return "Your conditions"
    return f"Option {index}: {'met' if group.passed else 'not met'}"


def _text(
    *,
    title: str,
    channel: str,
    permalink: str | None,
    after_hours: float,
    groups: list[GroupResult],
    metrics: Mapping[str, float],
) -> str:
    where = permalink or "(no link available - open the reel from your profile)"
    parts = [
        f'Your trial reel "{_short(title)}" on {channel} cleared its bar {_hours(after_hours)} after posting.',
        f"Open it: {where}",
    ]
    glance = [f"{label}: {_stat(key, metrics[key])}" for key, label in _HEADLINE if key in metrics]
    if glance:
        parts.append("At a glance\n" + "\n".join(f"  {g}" for g in glance))
    for i, g in enumerate(groups, start=1):
        parts.append(f"{_group_heading(i, g, len(groups))}\n" + "\n".join(f"  - {_line(r)}" for r in g.results))
    parts.append(
        "To graduate it\n"
        "  1. Open the reel in Instagram (the link above)\n"
        "  2. Tap the three dots\n"
        "  3. Choose to share it with your followers"
    )
    return "\n\n".join(parts) + "\n"


def _stat_cell(label: str, value: str) -> str:
    return (
        f'<td width="33%" align="center" style="padding:10px 4px;">'
        f'<div style="font:700 20px {_FONT};color:{_INK};">{escape(value)}</div>'
        f'<div style="font:500 11px {_FONT};color:{_MUTED};text-transform:uppercase;letter-spacing:.06em;'
        f'padding-top:2px;">{escape(label)}</div></td>'
    )


def _stats_grid(metrics: Mapping[str, float]) -> str:
    cells = [_stat_cell(label, _stat(key, metrics[key])) for key, label in _HEADLINE if key in metrics]
    if not cells:
        return ""
    rows = []
    for i in range(0, len(cells), 3):
        chunk = cells[i : i + 3]
        chunk += ['<td width="33%"></td>'] * (3 - len(chunk))
        rows.append(f"<tr>{''.join(chunk)}</tr>")
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-top:1px solid {_LINE};border-bottom:1px solid {_LINE};margin:20px 0;">{"".join(rows)}</table>'
    )


def _condition_row(r: ConditionResult) -> str:
    mark, colour = ("✓", _GOOD) if r.passed else ("✗", _BAD)
    label = METRIC_LABELS.get(r.metric, r.metric)
    return (
        f"<tr>"
        f'<td width="24" valign="top" style="padding:6px 0;font:700 14px {_FONT};color:{colour};">{mark}</td>'
        f'<td style="padding:6px 0;font:400 14px {_FONT};color:{_INK};">{escape(label)}'
        f'<span style="color:{_MUTED};"> &middot; needed {escape(OPERATOR_WORDS[r.op])} {escape(_number(r.value))}</span></td>'
        f'<td align="right" style="padding:6px 0;font:600 14px {_FONT};color:{_INK};">{escape(_number(r.actual))}</td>'
        f"</tr>"
    )


def _group_block(index: int, group: GroupResult, total: int) -> str:
    rows = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(_condition_row(r) for r in group.results)}</table>'
    if total == 1:
        return rows
    colour, bg = (_GOOD, _GOOD_BG) if group.passed else (_MUTED, _PAGE)
    badge = (
        f'<span style="font:600 11px {_FONT};color:{colour};background:{bg};border-radius:999px;'
        f'padding:2px 8px;margin-left:8px;">{"MET" if group.passed else "NOT MET"}</span>'
    )
    return (
        f'<div style="font:600 12px {_FONT};color:{_MUTED};text-transform:uppercase;letter-spacing:.06em;'
        f'padding:14px 0 4px;">Option {index}{badge}</div>{rows}'
    )


def _step(number: int, text: str) -> str:
    return (
        f"<tr>"
        f'<td width="30" valign="top" style="padding:5px 0;"><div style="width:22px;height:22px;border-radius:11px;'
        f'background:{_PAGE};color:{_ACCENT};font:700 12px/22px {_FONT};text-align:center;">{number}</div></td>'
        f'<td style="padding:5px 0;font:400 14px/1.5 {_FONT};color:{_INK};">{escape(text)}</td></tr>'
    )


def _html(
    *,
    title: str,
    channel: str,
    permalink: str | None,
    after_hours: float,
    groups: list[GroupResult],
    thumbnail_url: str | None,
    metrics: Mapping[str, float],
    story_html: str = "",
) -> str:
    safe_title = escape(_short(title))
    preheader = f"Cleared its bar {_hours(after_hours)} after posting. Open it and share it with your followers."

    thumb = (
        f'<td width="96" valign="top" style="padding-right:16px;">'
        f'<img src="{escape(thumbnail_url, quote=True)}" width="96" height="128" alt="" '
        f'style="display:block;width:96px;height:128px;object-fit:cover;border-radius:10px;'
        f'background:{_PAGE};border:0;"></td>'
        if thumbnail_url
        else ""
    )
    button = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:22px 0 4px;"><tr>'
        f'<td bgcolor="{_ACCENT}" style="border-radius:10px;">'
        f'<a href="{escape(permalink, quote=True)}" style="display:inline-block;padding:14px 26px;'
        f'font:600 15px {_FONT};color:#ffffff;text-decoration:none;border-radius:10px;">Open the reel in Instagram</a>'
        f"</td></tr></table>"
        if permalink
        else f'<p style="font:400 14px {_FONT};color:{_MUTED};margin:20px 0 4px;">'
        f"No link was available. Open the reel from your {escape(channel)} profile.</p>"
    )
    blocks = "".join(_group_block(i, g, len(groups)) for i, g in enumerate(groups, start=1))
    steps = "".join(
        _step(n, t)
        for n, t in enumerate(
            (
                "Tap the button above to open the reel in Instagram.",
                "Tap the three dots on the reel.",
                "Choose to share it with your followers.",
            ),
            start=1,
        )
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>Trial reel ready to graduate</title></head>
<body style="margin:0;padding:0;background:{_PAGE};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{_PAGE};">{escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{_PAGE}"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;">
  <tr><td style="padding:0 4px 14px;font:700 13px {_FONT};color:{_MUTED};letter-spacing:.08em;text-transform:uppercase;">{escape(channel)}</td></tr>
  <tr><td bgcolor="{_CARD}" style="background:{_CARD};border-radius:16px;padding:28px 24px;border:1px solid {_LINE};">
    <span style="display:inline-block;font:700 11px {_FONT};color:{_GOOD};background:{_GOOD_BG};border-radius:999px;padding:4px 10px;letter-spacing:.06em;">READY TO GRADUATE</span>
    <h1 style="font:700 22px/1.3 {_FONT};color:{_INK};margin:14px 0 6px;">Your trial reel cleared its bar</h1>
    <p style="font:400 14px/1.5 {_FONT};color:{_MUTED};margin:0 0 18px;">{escape(_hours(after_hours))} after posting, it met the conditions you set.</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{thumb}
      <td valign="top" style="font:600 15px/1.45 {_FONT};color:{_INK};">{safe_title}</td></tr></table>
    {button}
    {story_html}
    {_stats_grid(metrics)}
    <div style="font:700 13px {_FONT};color:{_INK};">What it had to reach</div>
    {blocks}
    <div style="font:700 13px {_FONT};color:{_INK};padding-top:24px;">To graduate it</div>
    <table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:6px;">{steps}</table>
  </td></tr>
  <tr><td align="center" style="padding:16px 8px;font:400 12px/1.5 {_FONT};color:{_MUTED};">
    Instagram only lets you share a trial reel from its own app, so this email is the nudge.
  </td></tr>
</table></td></tr></table></body></html>"""


def format_graduate_email(
    *,
    title: str,
    channel: str,
    permalink: str | None,
    after_hours: float,
    groups: list[GroupResult],
    thumbnail_url: str | None = None,
    metrics: Mapping[str, float] | None = None,
    story_html: str = "",
    story_text: str = "",
) -> TrialEmail:
    """Subject and both bodies. The link is the whole point: it opens the reel on a phone.

    ``story_html``/``story_text`` carry the reel's story card when it has one: a trial gets no story when
    it is posted, so this email is where the owner is handed the story kit.
    """
    numbers = metrics or {}
    return TrialEmail(
        subject=f"Trial reel ready to graduate: {_short(title, _MAX_SUBJECT_TITLE)}",
        text=_text(
            title=title, channel=channel, permalink=permalink, after_hours=after_hours, groups=groups, metrics=numbers
        )
        + ("\n" + story_text if story_text else ""),
        html=_html(
            title=title,
            channel=channel,
            permalink=permalink,
            after_hours=after_hours,
            groups=groups,
            thumbnail_url=thumbnail_url,
            metrics=numbers,
            story_html=story_html,
        ),
    )
