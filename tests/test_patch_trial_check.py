"""PATCH /videos/{id} with a trial_check (6 Nov 2026: a 500, "'dict' object has no attribute 'model_dump'").

The router dumps the whole body to plain data before calling the service, so trial_fields() gets the check
as a dict, not a TrialCheck. It must accept both and store the same thing.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.main import app
from app.routers import videos as router_mod
from app.services.trial_graduation import TrialCheck, trial_fields
from tests.conftest import GLOBAL_API_KEY

CHECK = {"after_hours": 24, "match": "all", "conditions": [{"metric": "views", "minimum": 1000}]}


def test_trial_fields_takes_the_check_as_a_dict_or_a_model():
    as_model = trial_fields("MANUAL", TrialCheck(**CHECK), None, "instagram")
    as_dict = trial_fields("MANUAL", TrialCheck(**CHECK).model_dump(), None, "instagram")
    assert as_dict == as_model
    assert as_dict["trial_check"]["check_until_hours"] == 24


def test_patch_with_a_trial_check_reaches_trial_fields_without_a_500():
    seen: dict[str, Any] = {}

    class FakeService:
        async def update_pending_video(self, channel_id, video_id, **changes):
            # what the real update_pending_video does with these two fields
            seen.update(trial_fields(changes.get("trial_graduation"), changes.get("trial_check"), None, "instagram"))
            return {"ok": True}

    app.dependency_overrides[router_mod.get_video_service] = FakeService
    try:
        r = TestClient(app, raise_server_exceptions=False).patch(
            "/api/v1/channels/c1/videos/v1",
            json={"trial_graduation": "MANUAL", "trial_check": CHECK},
            headers={"x-api-key": GLOBAL_API_KEY},
        )
    finally:
        app.dependency_overrides.pop(router_mod.get_video_service, None)
    assert r.status_code == 200, r.text
    assert seen["trial_graduation"] == "MANUAL"
    cond = seen["trial_check"]["groups"][0]["conditions"][0]
    assert cond["metric"] == "views" and cond["value"] == 1000
