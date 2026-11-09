from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.routers.views_series import _hourly_readings
from app.services.channel_follower_snapshots import record_follower_reading
from app.services.instagram_reel_hourly import HOUR, QUARTER, MetricReading
from app.services.view_series import hourly_points
from app.timezone import IST, now_ist


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_):
        return self

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d

        return gen()


class _Collection:
    def __init__(self):
        self.docs = []

    async def insert_one(self, doc):
        self.docs.append(doc)

    def find(self, query):
        return _Cursor(
            [d for d in self.docs if d["channel_id"] == query["channel_id"] and d["at"] >= query["at"]["$gte"]]
        )


@pytest.mark.asyncio
async def test_reading_is_stored_and_read_back_as_followers_metric():
    now = now_ist()
    coll = _Collection()

    class DB(SimpleNamespace):
        def __getitem__(self, name):
            return getattr(self, name)

    db = DB(channel_follower_snapshots=coll)
    await record_follower_reading(db, "c", 1000, now - timedelta(minutes=30))
    await record_follower_reading(db, "c", 1010, now)
    await record_follower_reading(db, "other", 5, now)

    readings, published = await _hourly_readings(db, "c", "instagram", None, "followers")
    assert [r.values["followers"] for r in readings] == [1000.0, 1010.0] and published is None


@pytest.mark.asyncio
async def test_per_video_followers_is_empty():
    db = SimpleNamespace()
    assert await _hourly_readings(db, "c", "instagram", "v1", "followers") == ([], None)


def test_quarter_hour_gain_follows_the_readings():
    # Readings on the quarter-hour slots: +5, +0, +10 followers in successive quarters.
    base = now_ist().astimezone(IST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    counts = [1000, 1005, 1005, 1015, 1015]
    readings = [MetricReading(at=base + QUARTER * i, values={"followers": float(c)}) for i, c in enumerate(counts)]
    now = base + QUARTER * 4 + timedelta(minutes=5)
    points = hourly_points(None, readings, "followers", now, 2, QUARTER)
    gains = [p.value for p in points if p.value is not None]
    assert gains == [5.0, 0.0, 10.0, 0.0]


def test_hourly_gain_sums_the_quarters():
    base = now_ist().astimezone(IST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    readings = [MetricReading(at=base + QUARTER * i, values={"followers": float(1000 + i * 2)}) for i in range(0, 9)]
    now = base + HOUR * 2 + timedelta(minutes=5)
    points = hourly_points(None, readings, "followers", now, 3, HOUR)
    assert [p.value for p in points if p.value is not None] == [8.0, 8.0]
