"""When is a manual trial reel good enough to graduate?

A trial reel set to ``MANUAL`` stays with non-followers until the owner shares it
from the Instagram app. The API cannot graduate it, so the best we can do is
watch the numbers and tell the owner when the reel is worth the tap.

The owner states the bar when scheduling: how long to keep checking (hours
after publish) and a set of *groups* of conditions. Every condition in a group
must hold; any one group holding is enough ("(skip rate under 50% AND over 500
views) OR over 2,000 views"). The reel is checked every hour until then; the
first check that passes ends it and sends the email, and if the last one still
falls short the reel is marked as having missed its bar. There is no "not
before" on purpose: a condition that needs volume to mean anything says so
itself (over 500 views AND skip rate under 50%). The clock starts at publish,
since a trial reel only exists once it is live.

Everything here is pure; the cron that reads Instagram and sends the email lives
in ``trial_graduation_cron``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.trial_reel import MANUAL, validate_strategy
from app.timezone import assume_utc

# Keys as stored by ``instagram_reel_metrics.reel_metadata``, so a check reads the
# same numbers the dashboard shows.
Metric = Literal["views", "reach", "likes", "comments", "saves", "shares", "avg_view_duration_seconds", "skip_rate"]

METRIC_LABELS: dict[str, str] = {
    "views": "Views",
    "reach": "Accounts reached",
    "likes": "Likes",
    "comments": "Comments",
    "saves": "Saves",
    "shares": "Shares",
    "avg_view_duration_seconds": "Average watch time (s)",
    "skip_rate": "Skip rate (%)",
}

# Instagram judges a trial over about 72 hours; a week is the most a check can
# sensibly run before the numbers stop meaning "trial performance".
MIN_UNTIL_HOURS = 0.25
MAX_UNTIL_HOURS = 168.0
CHECK_EVERY_HOURS = 1.0

PENDING = "pending"
MET = "met"
MISSED = "missed"
FAILED = "failed"


Operator = Literal["gte", "lte"]
OPERATOR_WORDS: dict[str, str] = {"gte": "at least", "lte": "at most"}

MAX_GROUPS = 5
MAX_CONDITIONS_PER_GROUP = 8


class Condition(BaseModel):
    metric: Metric
    # "lte" exists for numbers where lower is better, like the skip rate.
    op: Operator = "gte"
    value: float = Field(..., ge=0)


class ConditionGroup(BaseModel):
    """Conditions that must all hold together."""

    conditions: list[Condition] = Field(..., min_length=1, max_length=MAX_CONDITIONS_PER_GROUP)


class TrialCheck(BaseModel):
    """The bar a trial reel has to clear, and how long it is checked for, as the owner set it."""

    # Checked every hour after publish until this many hours have passed.
    check_until_hours: float
    # Any one group holding is enough.
    groups: list[ConditionGroup] = Field(..., min_length=1, max_length=MAX_GROUPS)

    @model_validator(mode="before")
    @classmethod
    def _read_older_shapes(cls, raw: Any) -> Any:
        return upgrade_check(raw)

    @field_validator("check_until_hours")
    @classmethod
    def _until_is_in_range(cls, hours: float) -> float:
        if hours < MIN_UNTIL_HOURS or hours > MAX_UNTIL_HOURS:
            raise ValueError(f"Check for between {MIN_UNTIL_HOURS} and {MAX_UNTIL_HOURS:g} hours after posting")
        return hours


def upgrade_check(raw: Any) -> Any:
    """Read a check saved in an older shape as the current one.

    - Conditions as one flat list (``match`` all/any, ``minimum``) become groups:
      "all" is one group holding every condition, "any" one group per condition.
    - Fixed check times (``after_hours``, one number or up to three) become
      hourly checks until the last of them, which keeps every moment the owner
      asked about inside the window.

    Anything already in the current shape, or not a mapping at all, is returned as it came.
    """
    if not isinstance(raw, Mapping):
        return raw
    upgraded = dict(raw)
    if "groups" not in upgraded and "conditions" in upgraded:
        flat = [{"metric": c["metric"], "op": "gte", "value": c["minimum"]} for c in upgraded.pop("conditions")]
        match = upgraded.pop("match", "all")
        upgraded["groups"] = [{"conditions": flat}] if match == "all" else [{"conditions": [c]} for c in flat]
    if "check_until_hours" not in upgraded and "after_hours" in upgraded:
        hours = upgraded.pop("after_hours")
        upgraded["check_until_hours"] = max(hours) if isinstance(hours, list) and hours else hours
    upgraded.pop("after_hours", None)
    return upgraded


def check_until_hours(check: Mapping[str, Any]) -> float:
    """How long a stored check runs, in hours after publish, whatever shape it was saved in."""
    return TrialCheck.model_validate(check).check_until_hours


def check_ends_at(video: Mapping[str, Any]) -> datetime:
    """The moment a reel's check stops: publish time plus its "check until" hours."""
    return assume_utc(video["published_at"]) + timedelta(hours=check_until_hours(video["trial_check"]))


def trial_fields(
    strategy_arg: str | None,
    check_arg: TrialCheck | None,
    existing_strategy: str | None,
    platform: str,
) -> dict[str, Any]:
    """The video fields to write for a schedule request, or ``{}`` if it says nothing.

    ``strategy_arg`` of ``None`` means "leave it" and ``""`` means "not a trial
    reel"; leaving it must not wipe an earlier choice. A check without a manual
    trial reel is refused: an automatic trial graduates itself and a normal reel
    has nothing to graduate.
    """
    fields: dict[str, Any] = {}
    strategy = existing_strategy
    if strategy_arg is not None:
        strategy = validate_strategy(strategy_arg, platform)
        fields["trial_graduation"] = strategy
        if strategy != MANUAL:
            fields["trial_check"] = None
    if check_arg is not None:
        if strategy != MANUAL:
            raise ValueError("A graduation check only applies to a trial reel you share yourself")
        fields["trial_check"] = check_arg.model_dump()
    return fields


def arm_fields(video: Mapping[str, Any], published_at: datetime) -> dict[str, Any]:
    """The fields that start the checks, written in the same update that marks the reel published."""
    check = video.get("trial_check")
    if video.get("trial_graduation") != MANUAL or not check:
        return {}
    first = min(CHECK_EVERY_HOURS, check_until_hours(check))
    return {
        "trial_check_state": PENDING,
        "trial_check_due_at": published_at + timedelta(hours=first),
    }


def next_check_at(video: Mapping[str, Any], now: datetime) -> datetime | None:
    """When to check next after a check at ``now`` fell short; ``None`` once the window has closed.

    An hour on, but never past the end: the last check lands exactly when the
    owner said to stop, so a reel crossing the bar in the final hour still counts.
    """
    end = check_ends_at(video)
    if now >= end:
        return None
    return min(now + timedelta(hours=CHECK_EVERY_HOURS), end)


class ConditionResult(BaseModel):
    metric: str
    op: Operator
    value: float
    actual: float | None
    passed: bool


class GroupResult(BaseModel):
    passed: bool
    results: list[ConditionResult]


class Evaluation(BaseModel):
    met: bool
    groups: list[GroupResult]


def _holds(op: str, actual: float, value: float) -> bool:
    return actual >= value if op == "gte" else actual <= value


def evaluate(check: Mapping[str, Any], metrics: Mapping[str, float]) -> Evaluation:
    """Compare a reel's numbers to the bar. A metric Instagram did not report cannot pass."""
    parsed = TrialCheck.model_validate(check)
    groups: list[GroupResult] = []
    for group in parsed.groups:
        results: list[ConditionResult] = []
        for cond in group.conditions:
            actual = metrics.get(cond.metric)
            results.append(
                ConditionResult(
                    metric=cond.metric,
                    op=cond.op,
                    value=cond.value,
                    actual=None if actual is None else float(actual),
                    passed=actual is not None and _holds(cond.op, float(actual), cond.value),
                )
            )
        groups.append(GroupResult(passed=all(r.passed for r in results), results=results))
    return Evaluation(met=any(g.passed for g in groups), groups=groups)


def edit_running_check(video: Mapping[str, Any], new_check: TrialCheck, now: datetime) -> dict[str, Any]:
    """The fields to write when the owner changes the check of a reel that is already live.

    Only a check still running can change, and its new end must still be ahead:
    an end already past would leave nothing to check. The next hourly check keeps
    its slot unless the new end comes sooner.
    """
    if video.get("trial_graduation") != MANUAL or not video.get("trial_check"):
        raise ValueError("This reel has no graduate check to edit")
    state = video.get("trial_check_state")
    if state != PENDING:
        raise ValueError("This reel's check has already finished, so it can no longer be changed")
    candidate = {**video, "trial_check": new_check.model_dump()}
    end = check_ends_at(candidate)
    if end <= now:
        elapsed = (now - assume_utc(video["published_at"])).total_seconds() / 3600
        raise ValueError(f"The reel has been up {elapsed:.0f}h already; check until a later hour than that")
    due = video.get("trial_check_due_at")
    next_at = min(assume_utc(due), end) if isinstance(due, datetime) else min(now, end)
    return {
        "trial_check": new_check.model_dump(),
        "trial_check_due_at": next_at,
        "trial_check_attempts": 0,
    }
