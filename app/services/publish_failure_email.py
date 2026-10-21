"""The email sent when a scheduled upload or publish gives up. Pure: text in, text out.

Silence was the failure it replaces: a video bounced back to "ready" looks the same in
the queue as one that was just produced, so nobody learned it had not gone out.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import NamedTuple

from app.services.publish_failure import GENERIC_REASON

_INK = "#0f172a"
_MUTED = "#64748b"
_LINE = "#e2e8f0"
_PAGE = "#f1f5f9"
_BAD = "#b91c1c"
_BAD_BG = "#fee2e2"
_ACCENT = "#2563eb"
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MAX_TITLE = 100


class FailureEmail(NamedTuple):
    subject: str
    text: str
    html: str


def _short(title: str) -> str:
    one_line = " ".join(title.split())
    return one_line if len(one_line) <= _MAX_TITLE else one_line[: _MAX_TITLE - 1].rstrip() + "…"


def _noun(platform: str, stage: str) -> tuple[str, str]:
    """("Reel", "publish") for Instagram, ("Video", "upload") for YouTube."""
    return ("Reel", "publish") if platform == "instagram" else ("Video", stage)


def format_failure_email(
    *,
    title: str,
    channel: str,
    platform: str,
    stage: str,
    reason: str,
    detail: str,
    attempts: int,
    scheduled_at: datetime | None,
    app_url: str,
) -> FailureEmail:
    """Subject and both bodies, led by the platform's own words about what went wrong."""
    noun, verb = _noun(platform, stage)
    name = _short(title)
    when = scheduled_at.strftime("%d %b, %H:%M") + " IST" if scheduled_at else "not set"
    # A vague reason ("Upload or publish failed") is no help; the platform's own words are the real answer.
    # Otherwise the detail is only worth a second line when it adds something the reason does not say.
    if reason == GENERIC_REASON and detail:
        reason, said = detail, ""
    else:
        said = detail if detail and detail != reason else ""
    todo = "Fix the cause, then schedule it again from the app. It is back in your ready videos."

    text = (
        f'{noun} "{name}" on {channel} did not {verb}.\n\n'
        f"What went wrong: {reason}\n" + (f"Platform said: {said}\n" if said else "") + f"Tried: {attempts} times\n"
        f"Was due: {when}\n\n{todo}\nOpen the app: {app_url}\n"
    )
    said_html = (
        f'<div style="margin-top:8px;font:400 14px/1.5 {_FONT};color:{_INK};">{escape(said)}</div>' if said else ""
    )
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(noun)} did not {escape(verb)}</title></head>
<body style="margin:0;padding:0;background:{_PAGE};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="{_PAGE}"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;">
  <tr><td style="padding:0 4px 14px;font:700 13px {_FONT};color:{_MUTED};letter-spacing:.08em;text-transform:uppercase;">{escape(channel)}</td></tr>
  <tr><td bgcolor="#ffffff" style="background:#ffffff;border-radius:16px;padding:28px 24px;border:1px solid {_LINE};">
    <span style="display:inline-block;font:700 11px {_FONT};color:{_BAD};background:{_BAD_BG};border-radius:999px;padding:4px 10px;letter-spacing:.06em;">DID NOT {escape(verb.upper())}</span>
    <h1 style="font:700 22px/1.3 {_FONT};color:{_INK};margin:14px 0 6px;">{escape(noun)} did not go out</h1>
    <p style="font:600 15px/1.45 {_FONT};color:{_INK};margin:0 0 18px;">{escape(name)}</p>
    <div style="border-left:3px solid {_BAD};background:{_PAGE};border-radius:8px;padding:12px 14px;">
      <div style="font:700 11px {_FONT};color:{_MUTED};text-transform:uppercase;letter-spacing:.06em;">What went wrong</div>
      <div style="margin-top:4px;font:600 15px/1.4 {_FONT};color:{_INK};">{escape(reason)}</div>{said_html}
    </div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0;">
      <tr><td style="padding:4px 0;font:400 14px {_FONT};color:{_MUTED};">Attempts</td><td align="right" style="font:600 14px {_FONT};color:{_INK};">{attempts}</td></tr>
      <tr><td style="padding:4px 0;font:400 14px {_FONT};color:{_MUTED};">Was due</td><td align="right" style="font:600 14px {_FONT};color:{_INK};">{escape(when)}</td></tr>
    </table>
    <p style="font:400 14px/1.5 {_FONT};color:{_INK};margin:0 0 16px;">{escape(todo)}</p>
    <table role="presentation" cellpadding="0" cellspacing="0"><tr><td bgcolor="{_ACCENT}" style="border-radius:10px;">
      <a href="{escape(app_url, quote=True)}" style="display:inline-block;padding:13px 24px;font:600 15px {_FONT};color:#ffffff;text-decoration:none;border-radius:10px;">Open the app</a>
    </td></tr></table>
  </td></tr>
</table></td></tr></table></body></html>"""
    return FailureEmail(subject=f"{noun} did not {verb}: {name} ({channel})", text=text, html=html)
