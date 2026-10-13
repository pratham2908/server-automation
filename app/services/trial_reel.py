"""Trial reels: a reel shown only to non-followers until it proves itself.

Meta exposes this on reel container creation as ``trial_params``. The one
choice is how the reel leaves the trial ("graduates") to reach followers:

* ``MANUAL`` - the owner graduates it by hand in the Instagram app.
* ``SS_PERFORMANCE`` - Instagram graduates it automatically if it performs well.

Graduation itself is not exposed by the API, so ``MANUAL`` really means "in the app".
"""

from __future__ import annotations

import json

MANUAL = "MANUAL"
SS_PERFORMANCE = "SS_PERFORMANCE"
STRATEGIES: tuple[str, ...] = (MANUAL, SS_PERFORMANCE)


def validate_strategy(value: str | None, platform: str) -> str | None:
    """Normalise a requested graduation strategy, or raise if it cannot be used.

    ``None`` and blank both mean "not a trial reel", so the caller can clear the
    setting by sending an empty string.
    """
    if value is None:
        return None
    cleaned = value.strip().upper()
    if not cleaned:
        return None
    if platform != "instagram":
        raise ValueError("Trial reels are an Instagram feature")
    if cleaned not in STRATEGIES:
        raise ValueError(f"Trial reel graduation must be one of {', '.join(STRATEGIES)}")
    return cleaned


def trial_params_json(strategy: str | None) -> str | None:
    """The ``trial_params`` value Meta expects: a JSON object sent as a form field."""
    if not strategy:
        return None
    return json.dumps({"graduation_strategy": strategy})


# Meta's error_subcode for "this account has too few followers to make trial reels".
NOT_ENOUGH_FOLLOWERS_SUBCODE = 2207081


class TrialReelNotAvailableError(RuntimeError):
    """The account is not allowed to make trial reels (yet)."""


def explain_refusal(error_body: object) -> str | None:
    """A readable reason when Meta refused a trial reel for an account-level cause, else ``None``.

    This must surface as a failure rather than quietly publishing a normal reel:
    the whole point of a trial is that followers do not see the video.
    """
    if not isinstance(error_body, dict):
        return None
    error = error_body.get("error")
    if isinstance(error, dict) and error.get("error_subcode") == NOT_ENOUGH_FOLLOWERS_SUBCODE:
        return "This Instagram account does not have enough followers to publish trial reels yet"
    return None
