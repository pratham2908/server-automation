"""Fallback videos: if a reel misses its bar, publish a video the owner picked in its place.

The owner attaches a rule to one reel (scheduled or already posted): which ready
video is its fallback, and the bar the reel has to clear - the same bar as a
trial reel's graduate check (groups of conditions, checked every hour after
publish until "check until" hours). The first hourly check that clears the bar
ends it and the fallback stays unused; if the last check still falls short, the
fallback is queued to publish a couple of minutes later.

Rule and progress live on the reel's own video document, so a restart loses
nothing. The fallback video carries ``fallback_for`` (the reel it backs up) so
the auto-scheduler leaves it alone while the rule is waiting.

Everything here is pure; the cron that reads Instagram and publishes lives in
``fallback_video_cron``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from app.services.trial_graduation import CHECK_EVERY_HOURS, TrialCheck
from app.timezone import assume_utc

PENDING = "pending"  # waiting for the reel to publish, or being checked hourly
MET = "met"  # the reel cleared its bar; the fallback was not needed
TRIGGERED = "triggered"  # the reel missed; the fallback is queued to publish
FAILED = "failed"  # could not check the reel, or could not queue the fallback

# The fallback goes out this long after the miss, so the publisher picks it up on its next pass.
PUBLISH_DELAY = timedelta(minutes=2)

# Statuses a reel can have while a rule on it still means something.
RULE_STATUSES = ("ready", "scheduled", "queued", "published")


class FallbackRule(BaseModel):
    fallback_video_id: str = Field(..., min_length=1)
    check: TrialCheck


def ends_at(published_at: datetime, check: Mapping[str, Any]) -> datetime:
    """When the last check runs: publish time plus the bar's "check until" hours."""
    return assume_utc(published_at) + timedelta(hours=TrialCheck.model_validate(check).check_until_hours)


def first_due(published_at: datetime, check: Mapping[str, Any]) -> datetime:
    """The first check: an hour after publish, or the end of the window if that comes sooner."""
    return min(assume_utc(published_at) + timedelta(hours=CHECK_EVERY_HOURS), ends_at(published_at, check))


def next_due(published_at: datetime, check: Mapping[str, Any], now: datetime) -> datetime | None:
    """An hour on from a check that fell short, never past the end; ``None`` once the window has closed."""
    end = ends_at(published_at, check)
    if now >= end:
        return None
    return min(now + timedelta(hours=CHECK_EVERY_HOURS), end)


def rule_fields(
    reel: Mapping[str, Any],
    fallback: Mapping[str, Any] | None,
    rule: FallbackRule,
    platform: str,
    now: datetime,
) -> dict[str, Any]:
    """The fields that put *rule* on *reel*, or ``ValueError`` saying why it can't go there."""
    if platform != "instagram":
        raise ValueError("Fallback videos are for Instagram reels")
    if reel.get("status") not in RULE_STATUSES:
        raise ValueError(f"This reel is '{reel.get('status')}', so it can't have a fallback")
    if reel.get("fallback_state") in (TRIGGERED, MET):
        raise ValueError("This reel's fallback check has already finished")
    if fallback is None:
        raise ValueError("Fallback video not found on this channel")
    if fallback["video_id"] == reel["video_id"]:
        raise ValueError("A reel can't be its own fallback")
    if fallback.get("status") != "ready":
        raise ValueError(f"The fallback must be a ready video; this one is '{fallback.get('status')}'")
    if not fallback.get("r2_object_key"):
        raise ValueError("The fallback video has no uploaded file to publish")
    taken = fallback.get("fallback_for")
    if taken and taken != reel["video_id"]:
        raise ValueError("That video is already the fallback for another reel")
    check = rule.check.model_dump()
    fields: dict[str, Any] = {
        "fallback": {"video_id": fallback["video_id"], "check": check},
        "fallback_state": PENDING,
        "fallback_due_at": None,
        "fallback_attempts": 0,
        "fallback_error": None,
    }
    published_at = reel.get("published_at") if reel.get("status") == "published" else None
    if isinstance(published_at, datetime):
        end = ends_at(published_at, check)
        if end <= now:
            elapsed = (now - assume_utc(published_at)).total_seconds() / 3600
            raise ValueError(f"The reel has been up {elapsed:.0f}h already; check until a later hour than that")
        due = assume_utc(reel["fallback_due_at"]) if isinstance(reel.get("fallback_due_at"), datetime) else None
        # A rule set on a live reel is checked at its next hourly slot, or right away if that has passed.
        fields["fallback_due_at"] = max(min(due or first_due(published_at, check), end), now)
    return fields


def miss_summary(result: Mapping[str, Any] | None) -> str:
    """One line on how the reel fell short, from a stored evaluation."""
    groups = (result or {}).get("groups") or []
    parts = []
    for group in groups:
        for r in group.get("results", []):
            actual = "n/a" if r.get("actual") is None else f"{r['actual']:g}"
            word = "≥" if r.get("op") == "gte" else "≤"
            parts.append(f"{r['metric']} {actual} (needed {word} {r['value']:g})")
    return "; ".join(parts)
