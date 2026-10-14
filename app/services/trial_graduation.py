"""When is a manual trial reel good enough to graduate?

A trial reel set to ``MANUAL`` stays with non-followers until the owner shares it
from the Instagram app. The API cannot graduate it, so the best we can do is
watch the numbers and tell the owner when the reel is worth the tap.

The owner states the bar when scheduling: up to three check times (hours after
publish) and a set of *groups* of conditions. Every condition in a group must
hold; any one group holding is enough ("(skip rate under 50% AND over 500 views)
OR over 2,000 views"). The first check that passes ends it and sends the email;
if the last check still falls short the reel is marked as having missed its bar.
The clock starts at publish, since a trial reel only exists once it is live.

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
# sensibly wait before the numbers stop meaning "trial performance".
MIN_AFTER_HOURS = 0.25
MAX_AFTER_HOURS = 168.0
MAX_TIMERS = 3

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
    """The bar a trial reel has to clear, and when it is looked at, as the owner set it."""

    after_hours: list[float] = Field(..., min_length=1, max_length=MAX_TIMERS)
    # Any one group holding is enough.
    groups: list[ConditionGroup] = Field(..., min_length=1, max_length=MAX_GROUPS)

    @model_validator(mode="before")
    @classmethod
    def _read_older_shapes(cls, raw: Any) -> Any:
        return upgrade_check(raw)

    @field_validator("after_hours", mode="before")
    @classmethod
    def _single_hours_is_one_timer(cls, value: Any) -> Any:
        # A check saved before there could be several stored one number.
        return [value] if isinstance(value, int | float) else value

    @field_validator("after_hours")
    @classmethod
    def _timers_are_in_range_and_ascending(cls, hours: list[float]) -> list[float]:
        if any(h < MIN_AFTER_HOURS or h > MAX_AFTER_HOURS for h in hours):
            raise ValueError(f"Each check must be between {MIN_AFTER_HOURS} and {MAX_AFTER_HOURS} hours after posting")
        if any(later <= earlier for earlier, later in zip(hours, hours[1:], strict=False)):
            raise ValueError("Check times must each be later than the one before")
        return hours


def upgrade_check(raw: Any) -> Any:
    """Turn a check saved as one flat list (``match`` all/any, ``minimum``) into groups.

    "all" becomes one group holding every condition; "any" becomes one group per
    condition, since any single one passing is enough. Anything already in the
    current shape, or not a mapping at all, is returned as it came.
    """
    if not isinstance(raw, Mapping) or "groups" in raw or "conditions" not in raw:
        return raw
    flat = [{"metric": c["metric"], "op": "gte", "value": c["minimum"]} for c in raw["conditions"]]
    groups = [{"conditions": flat}] if raw.get("match", "all") == "all" else [{"conditions": [c]} for c in flat]
    return {"after_hours": raw["after_hours"], "groups": groups}


def check_hours(check: Mapping[str, Any]) -> list[float]:
    """The check times of a stored check, whether it was saved with one or several."""
    return list(TrialCheck.model_validate(check).after_hours)


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
    """The fields that start the timer, written in the same update that marks the reel published."""
    check = video.get("trial_check")
    if video.get("trial_graduation") != MANUAL or not check:
        return {}
    return {
        "trial_check_state": PENDING,
        "trial_check_stage": 0,
        "trial_check_due_at": published_at + timedelta(hours=check_hours(check)[0]),
    }


def next_due(video: Mapping[str, Any], stage: int) -> datetime | None:
    """When timer ``stage`` falls due (counted from publish), or ``None`` if there is no such timer."""
    hours = check_hours(video["trial_check"])
    if stage >= len(hours):
        return None
    return assume_utc(video["published_at"]) + timedelta(hours=hours[stage])


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


def edit_running_check(video: Mapping[str, Any], new_check: TrialCheck) -> dict[str, Any]:
    """The fields to write when the owner changes the check of a reel that is already live.

    Only a check still waiting on a timer can change. The timers already run are
    history, so they cannot move, and at least one must remain to run. The due time
    is recomputed from the publish time for the timer that is next in line.
    """
    if video.get("trial_graduation") != MANUAL or not video.get("trial_check"):
        raise ValueError("This reel has no graduate check to edit")
    state = video.get("trial_check_state")
    if state != PENDING:
        raise ValueError("This reel's check has already finished, so it can no longer be changed")
    stage = int(video.get("trial_check_stage", 0))
    old_hours = check_hours(video["trial_check"])
    new_hours = new_check.after_hours
    if new_hours[:stage] != old_hours[:stage]:
        raise ValueError("Checks that have already run cannot be changed")
    if len(new_hours) <= stage:
        raise ValueError("Keep at least one check still to come")
    candidate = {**video, "trial_check": new_check.model_dump()}
    return {
        "trial_check": new_check.model_dump(),
        "trial_check_due_at": next_due(candidate, stage),
        "trial_check_attempts": 0,
    }
