"""The auto-scheduler schedules linked channels alongside the primary.

Linking a channel says "same brand, same content": the import already creates a
sibling video record on every linked channel, but nothing scheduled it, so a
linked channel accumulated ready videos and posted none of them.

The safety property under test is narrow on purpose: only *this video's* sibling
is scheduled. Reaching for whatever else was ready on the linked channel would
post unrelated content to a public account at the primary's slot time.
"""

from datetime import datetime

import pytest

import app.services.auto_scheduler_cron as cron
from app.timezone import IST

_AT = datetime(2026, 8, 26, 19, 0, tzinfo=IST)
_GROUP = "grp-1"

_PRIMARY = {"channel_id": "geo_yt", "name": "Geo Ranking", "platform": "youtube"}
_LINKED = {"channel_id": "geo_ig", "name": "Geo Ranking", "platform": "instagram"}


class FakeChannels:
    def __init__(self, docs):
        self._docs = {d["channel_id"]: d for d in docs}

    async def find_one(self, query, _projection=None):
        return self._docs.get(query.get("channel_id"))


class FakeVideos:
    def __init__(self, docs):
        self._docs = docs

    async def find_one(self, query, _projection=None):
        for d in self._docs:
            if all(d.get(k) == v for k, v in query.items()):
                return d
        return None


class FakeDB:
    def __init__(self, videos, channels=(_PRIMARY, _LINKED)):
        self.videos = FakeVideos(videos)
        self.channels = FakeChannels(list(channels))


def _patch(monkeypatch, targets, calls, status="queued"):
    """Route expansion_targets and capture every enqueue the expansion makes."""

    class FakeGroupService:
        def __init__(self, _db):
            pass

        async def expansion_targets(self, _channel_id):
            return list(targets)

    monkeypatch.setattr("app.services.channel_group_service.ChannelGroupService", FakeGroupService)

    async def fake_enqueue(_db, channel, video_doc, schedule_at):
        calls.append((channel["channel_id"], video_doc["video_id"], schedule_at))
        return {"status": status}

    monkeypatch.setattr(cron, "_enqueue_for_channel", fake_enqueue)


@pytest.mark.asyncio
async def test_the_linked_copy_is_scheduled_at_the_same_time(monkeypatch):
    db = FakeDB([{"channel_id": "geo_ig", "video_id": "sib", "multi_channel_group_id": _GROUP, "status": "ready"}])
    calls: list = []
    _patch(monkeypatch, ["geo_ig"], calls)

    out = await cron._schedule_linked_siblings(db, _PRIMARY, {"multi_channel_group_id": _GROUP}, _AT)

    assert calls == [("geo_ig", "sib", _AT)]
    assert out == [
        {"channel_id": "geo_ig", "channel_name": "Geo Ranking", "state": "scheduled", "video_id": "sib"}
    ]


@pytest.mark.asyncio
async def test_an_unrelated_ready_video_on_the_linked_channel_is_never_touched(monkeypatch):
    """The whole point: post the same video, not just anything that was ready."""
    db = FakeDB(
        [
            {"channel_id": "geo_ig", "video_id": "other", "multi_channel_group_id": "different", "status": "ready"},
            {"channel_id": "geo_ig", "video_id": "loose", "status": "ready"},
        ]
    )
    calls: list = []
    _patch(monkeypatch, ["geo_ig"], calls)

    out = await cron._schedule_linked_siblings(db, _PRIMARY, {"multi_channel_group_id": _GROUP}, _AT)

    assert calls == []
    assert out[0]["state"] == "skipped"
    assert "no linked copy" in out[0]["reason"]


@pytest.mark.asyncio
async def test_a_sibling_still_analysing_is_reported_not_scheduled(monkeypatch):
    db = FakeDB([{"channel_id": "geo_ig", "video_id": "sib", "multi_channel_group_id": _GROUP, "status": "processing"}])
    calls: list = []
    _patch(monkeypatch, ["geo_ig"], calls)

    out = await cron._schedule_linked_siblings(db, _PRIMARY, {"multi_channel_group_id": _GROUP}, _AT)

    assert calls == []
    assert out[0]["state"] == "skipped"
    assert "'processing'" in out[0]["reason"]


@pytest.mark.asyncio
async def test_auto_target_off_expands_to_nothing(monkeypatch):
    """expansion_targets returns [] for a label-only group; that must be honoured."""
    db = FakeDB([{"channel_id": "geo_ig", "video_id": "sib", "multi_channel_group_id": _GROUP, "status": "ready"}])
    calls: list = []
    _patch(monkeypatch, [], calls)

    assert await cron._schedule_linked_siblings(db, _PRIMARY, {"multi_channel_group_id": _GROUP}, _AT) == []
    assert calls == []


@pytest.mark.asyncio
async def test_a_video_with_no_group_expands_to_nothing(monkeypatch):
    db = FakeDB([])
    calls: list = []
    _patch(monkeypatch, ["geo_ig"], calls)

    assert await cron._schedule_linked_siblings(db, _PRIMARY, {}, _AT) == []
    assert calls == []


@pytest.mark.asyncio
async def test_a_failed_primary_schedules_no_linked_channel(monkeypatch):
    """Nothing on a linked channel goes out if the video itself did not."""
    db = FakeDB([{"channel_id": "geo_ig", "video_id": "sib", "multi_channel_group_id": _GROUP, "status": "ready"}])
    calls: list = []
    _patch(monkeypatch, ["geo_ig"], calls, status="failed")

    result = await cron._schedule_video(db, _PRIMARY, {"video_id": "main", "multi_channel_group_id": _GROUP}, _AT)

    assert result == {"status": "failed"}
    assert calls == [("geo_yt", "main", _AT)]  # the primary attempt, and nothing after it


@pytest.mark.asyncio
async def test_a_deleted_linked_channel_is_skipped_quietly(monkeypatch):
    db = FakeDB(
        [{"channel_id": "gone", "video_id": "sib", "multi_channel_group_id": _GROUP, "status": "ready"}],
        channels=(_PRIMARY,),
    )
    calls: list = []
    _patch(monkeypatch, ["gone"], calls)

    assert await cron._schedule_linked_siblings(db, _PRIMARY, {"multi_channel_group_id": _GROUP}, _AT) == []
    assert calls == []


# ------------------------------------------------------------------
# The summary has to admit what was posted
# ------------------------------------------------------------------


def test_summary_lists_a_linked_channel_as_its_own_scheduled_row():
    """Two channels posted, so the email must show two rows, not one."""
    runs = {
        "geo_yt": {
            "channel_id": "geo_yt",
            "channel_name": "Geo Ranking",
            "slots": {
                "19:00": {
                    "state": "scheduled",
                    "video_id": "main",
                    "linked": [
                        {
                            "channel_id": "geo_ig",
                            "channel_name": "Geo Ranking IG",
                            "state": "scheduled",
                            "video_id": "sib",
                        }
                    ],
                }
            },
        }
    }
    summary = cron._assemble_summary(datetime(2026, 8, 26).date(), runs)

    assert len(summary["scheduled"]) == 2
    linked = [row for row in summary["scheduled"] if row["channel_id"] == "geo_ig"][0]
    assert linked["slot"] == "19:00"
    assert linked["video_id"] == "sib"
    assert linked["source"] == "linked to Geo Ranking"


def test_summary_reports_a_linked_channel_that_was_skipped():
    runs = {
        "geo_yt": {
            "channel_id": "geo_yt",
            "channel_name": "Geo Ranking",
            "slots": {
                "19:00": {
                    "state": "scheduled",
                    "video_id": "main",
                    "linked": [
                        {
                            "channel_id": "geo_ig",
                            "channel_name": "Geo Ranking IG",
                            "state": "skipped",
                            "reason": "linked copy is 'processing', not ready",
                        }
                    ],
                }
            },
        }
    }
    summary = cron._assemble_summary(datetime(2026, 8, 26).date(), runs)

    assert len(summary["scheduled"]) == 1
    assert summary["skipped"][0]["channel_id"] == "geo_ig"
    assert "not ready" in summary["skipped"][0]["reason"]
