"""ChannelUpdate must declare every PATCH-able field.

`update_channel` applies `body.model_dump(exclude_none=True)`, so any field NOT
declared on ChannelUpdate is silently dropped — which is exactly how the pause
toggle was a no-op before `paused` (and `starred`) were added. These lock that
in so a future edit can't quietly drop them again.
"""

from __future__ import annotations

from app.routers.channels import ChannelUpdate


class TestChannelUpdateFields:
    def test_paused_and_starred_survive_dump(self):
        dumped = ChannelUpdate(paused=True, starred=True).model_dump(exclude_none=True)
        assert dumped == {"paused": True, "starred": True}

    def test_unpause_false_is_kept(self):
        # exclude_none keeps False (only None is dropped) so a channel can be un-paused.
        assert ChannelUpdate(paused=False).model_dump(exclude_none=True) == {"paused": False}

    def test_unset_fields_are_omitted(self):
        assert ChannelUpdate(name="x").model_dump(exclude_none=True) == {"name": "x"}

    def test_empty_update_is_empty(self):
        assert ChannelUpdate().model_dump(exclude_none=True) == {}
