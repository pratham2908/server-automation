"""The email that says a trial reel cleared its bar. Pure: text in, text out."""

from __future__ import annotations

from html import escape
from typing import NamedTuple

from app.services.trial_graduation import METRIC_LABELS, ConditionResult


class TrialEmail(NamedTuple):
    subject: str
    text: str
    html: str


def _number(value: float | None) -> str:
    if value is None:
        return "not reported"
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _line(r: ConditionResult) -> str:
    label = METRIC_LABELS.get(r.metric, r.metric)
    return f"{label}: {_number(r.actual)} (needed {_number(r.minimum)})"


def format_graduate_email(
    *, title: str, channel: str, permalink: str | None, after_hours: float, match: str, results: list[ConditionResult]
) -> TrialEmail:
    """Subject and both bodies. The link is the whole point: it opens the reel on a phone."""
    lines = [_line(r) for r in results]
    rule = "all of these" if match == "all" else "at least one of these"
    where = permalink or "(no link available - open the reel from your profile)"
    steps = "Open the reel in Instagram, tap the three dots, and share it with your followers."

    text = (
        f'Your trial reel "{title}" on {channel} cleared its bar {_hours(after_hours)} after posting '
        f"({rule}):\n\n" + "\n".join(f"  - {x}" for x in lines) + f"\n\nOpen it: {where}\n\n{steps}\n"
    )
    items = "".join(f"<li>{escape(x)}</li>" for x in lines)
    button = (
        f'<p><a href="{escape(permalink, quote=True)}">Open the reel in Instagram</a></p>'
        if permalink
        else f"<p>{escape(where)}</p>"
    )
    html = (
        f"<p>Your trial reel <strong>{escape(title)}</strong> on {escape(channel)} cleared its bar "
        f"{escape(_hours(after_hours))} after posting ({escape(rule)}):</p><ul>{items}</ul>{button}<p>{escape(steps)}</p>"
    )
    return TrialEmail(subject=f"Trial reel ready to graduate: {title}", text=text, html=html)


def _hours(hours: float) -> str:
    return f"{_number(hours)} hour{'' if hours == 1 else 's'}"
