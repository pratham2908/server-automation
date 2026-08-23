"""Pure formatting of the daily auto-scheduler run into an email.

Kept separate from the cron worker so it imports nothing with side effects and
stays trivially testable. The summary dict is assembled by the worker; shape:

    {
      "date": "2026-08-24",
      "scheduled": [{"channel_id", "channel_name"?, "slot", "video_id", "source"?}],
      "skipped":   [{"channel_id", "channel_name"?, "slot"?, "reason"}],
    }
"""

from __future__ import annotations

from typing import Any


def _label(entry: dict[str, Any]) -> str:
    """Prefer a human channel name, falling back to the id."""
    return str(entry.get("channel_name") or entry.get("channel_id") or "unknown")


def format_summary_email(summary: dict[str, Any]) -> tuple[str, str]:
    """Return ``(subject, body)`` for a run summary."""
    day = summary.get("date", "")
    scheduled = summary.get("scheduled", [])
    skipped = summary.get("skipped", [])

    subject = f"Auto-scheduler: {len(scheduled)} scheduled, {len(skipped)} skipped ({day})"

    lines: list[str] = [f"Auto-scheduler run for {day}", ""]

    lines.append(f"Scheduled ({len(scheduled)}):")
    if scheduled:
        for e in scheduled:
            slot = e.get("slot", "")
            source = f" from {e['source']}" if e.get("source") else ""
            lines.append(f"  - {_label(e)} @ {slot} → {e.get('video_id', '')}{source}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"Skipped ({len(skipped)}):")
    if skipped:
        for e in skipped:
            slot = f" @ {e['slot']}" if e.get("slot") else ""
            lines.append(f"  - {_label(e)}{slot} — {e.get('reason', '')}")
    else:
        lines.append("  (none)")

    return subject, "\n".join(lines)
