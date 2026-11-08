from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import storage_usage as su

GB = 1024**3
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


class FakeConfig:
    def __init__(self, doc=None):
        self.doc = doc

    async def find_one(self, query):
        return dict(self.doc) if self.doc else None

    async def update_one(self, query, update, upsert=False):
        self.doc = {**(self.doc or {}), **update["$set"]}


class FakeR2:
    def __init__(self, sizes):
        self.objects = [{"key": f"{ch}/f{i}.mp4", "size": n} for i, (ch, n) in enumerate(sizes)]

    def list_objects_with_prefix(self, prefix):
        return self.objects


def test_summarize_groups_by_channel():
    total, by = su.summarize(
        [{"key": "a/x.mp4", "size": 5}, {"key": "a/y.mp4", "size": 7}, {"key": "b/z.mp4", "size": 1}]
    )
    assert total == 13 and by == {"a": 12, "b": 1}


def test_should_alert_rules():
    assert not su.should_alert(None, NOW, is_over=False)
    assert su.should_alert(None, NOW, is_over=True)
    assert not su.should_alert(NOW - timedelta(days=3), NOW, is_over=True)
    assert su.should_alert(NOW - timedelta(days=7), NOW, is_over=True)


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_send(settings, recipient, subject, body, html=None):
        calls.append((recipient, subject))
        return True

    async def fake_owner(db, settings):
        return "owner@example.com"

    monkeypatch.setattr(su, "send_email", fake_send)
    monkeypatch.setattr(su, "resolve_owner_email", fake_owner)
    return calls


@pytest.mark.asyncio
async def test_under_limit_stores_reading_and_sends_nothing(sent):
    db = SimpleNamespace(config=FakeConfig())
    out = await su.refresh_storage_usage(db, FakeR2([("a", 2 * GB)]), SimpleNamespace(ANALYZER_PUBLIC_URL="u"), now=NOW)
    assert out["total_bytes"] == 2 * GB and out["over_threshold"] is False and sent == []


@pytest.mark.asyncio
async def test_crossing_emails_once_then_stays_quiet_until_reminder(sent):
    db = SimpleNamespace(config=FakeConfig())
    r2 = FakeR2([("a", 8 * GB)])
    s = SimpleNamespace(ANALYZER_PUBLIC_URL="u")
    out = await su.refresh_storage_usage(db, r2, s, now=NOW)
    assert out["over_threshold"] is True and len(sent) == 1
    await su.refresh_storage_usage(db, r2, s, now=NOW + timedelta(days=1))
    assert len(sent) == 1
    await su.refresh_storage_usage(db, r2, s, now=NOW + timedelta(days=8))
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_dropping_under_rearms_the_alert(sent):
    db = SimpleNamespace(config=FakeConfig())
    s = SimpleNamespace(ANALYZER_PUBLIC_URL="u")
    await su.refresh_storage_usage(db, FakeR2([("a", 8 * GB)]), s, now=NOW)
    await su.refresh_storage_usage(db, FakeR2([("a", 1 * GB)]), s, now=NOW + timedelta(days=1))
    await su.refresh_storage_usage(db, FakeR2([("a", 8 * GB)]), s, now=NOW + timedelta(days=2))
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_failed_send_is_retried_next_check(monkeypatch):
    attempts = []

    async def failing(settings, recipient, subject, body, html=None):
        attempts.append(1)
        return False

    async def owner(db, settings):
        return "o@example.com"

    monkeypatch.setattr(su, "send_email", failing)
    monkeypatch.setattr(su, "resolve_owner_email", owner)
    db = SimpleNamespace(config=FakeConfig())
    s = SimpleNamespace(ANALYZER_PUBLIC_URL="u")
    r2 = FakeR2([("a", 8 * GB)])
    await su.refresh_storage_usage(db, r2, s, now=NOW)
    await su.refresh_storage_usage(db, r2, s, now=NOW + timedelta(days=1))
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_cached_usage_empty_before_first_check():
    out = await su.get_cached_usage(SimpleNamespace(config=FakeConfig()))
    assert out["total_bytes"] is None and out["over_threshold"] is False and out["threshold_bytes"] == int(7.5 * GB)
