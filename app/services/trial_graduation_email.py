"""The email that says a trial reel cleared its bar. Pure: text in, text out."""

from __future__ import annotations

from html import escape
from typing import NamedTuple

from app.services.trial_graduation import METRIC_LABELS, OPERATOR_WORDS, ConditionResult, GroupResult


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
    word = OPERATOR_WORDS[r.op]
    return f"{label}: {_number(r.actual)} (needed {word} {_number(r.value)})"


def _group_blocks(groups: list[GroupResult]) -> list[tuple[str, list[str]]]:
    """One (heading, lines) per group, naming which ones passed when there is a choice."""
    blocks: list[tuple[str, list[str]]] = []
    for i, g in enumerate(groups, start=1):
        if len(groups) == 1:
            heading = "All of these were met" if g.passed else "These were the conditions"
        else:
            heading = f"Option {i}: {'met' if g.passed else 'not met'}"
        blocks.append((heading, [_line(r) for r in g.results]))
    return blocks


def format_graduate_email(
    *, title: str, channel: str, permalink: str | None, after_hours: float, groups: list[GroupResult]
) -> TrialEmail:
    """Subject and both bodies. The link is the whole point: it opens the reel on a phone."""
    blocks = _group_blocks(groups)
    where = permalink or "(no link available - open the reel from your profile)"
    steps = "Open the reel in Instagram, tap the three dots, and share it with your followers."
    intro = f'Your trial reel "{title}" on {channel} cleared its bar {_hours(after_hours)} after posting.'

    text = (
        intro
        + "\n\n"
        + "\n\n".join(f"{heading}:\n" + "\n".join(f"  - {x}" for x in lines) for heading, lines in blocks)
        + f"\n\nOpen it: {where}\n\n{steps}\n"
    )
    sections = "".join(
        f"<p><strong>{escape(heading)}</strong></p><ul>{''.join(f'<li>{escape(x)}</li>' for x in lines)}</ul>"
        for heading, lines in blocks
    )
    button = (
        f'<p><a href="{escape(permalink, quote=True)}">Open the reel in Instagram</a></p>'
        if permalink
        else f"<p>{escape(where)}</p>"
    )
    html = (
        f"<p>Your trial reel <strong>{escape(title)}</strong> on {escape(channel)} cleared its bar "
        f"{escape(_hours(after_hours))} after posting.</p>{sections}{button}<p>{escape(steps)}</p>"
    )
    return TrialEmail(subject=f"Trial reel ready to graduate: {title}", text=text, html=html)


def _hours(hours: float) -> str:
    return f"{_number(hours)} hour{'' if hours == 1 else 's'}"
