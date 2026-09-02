"""Shared test fixtures.

Settings are read from the environment at import time, so the dummy values below
must be in place before anything under ``app`` is imported.
"""

import os

_TEST_ENV = {
    "API_KEY": "test-global-key",
    "MONGODB_URI": "mongodb://localhost:27017",
    "MONGODB_DB_NAME": "test_youtube_automation",
    "R2_ACCOUNT_ID": "test-account",
    "R2_ACCESS_KEY_ID": "test-access-key",
    "R2_SECRET_ACCESS_KEY": "test-secret-key",
    "R2_BUCKET_NAME": "test-bucket",
    "R2_ENDPOINT_URL": "https://test.r2.cloudflarestorage.com",
}
for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)

GLOBAL_API_KEY = os.environ["API_KEY"]

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402


class FakeCollection:
    """Minimal in-memory stand-in for a Motor collection.

    Supports only the operations the channel API-key code paths use:
    equality-match ``find_one`` (with exclusion projections) and ``$set`` updates.
    """

    def __init__(self, docs: list[dict] | None = None):
        self._docs = [dict(d) for d in (docs or [])]

    @staticmethod
    def _matches(doc: dict, query: dict) -> bool:
        return all(doc.get(key) == value for key, value in query.items())

    async def find_one(self, query: dict, projection: dict | None = None) -> dict | None:
        for doc in self._docs:
            if self._matches(doc, query):
                found = dict(doc)
                for field, include in (projection or {}).items():
                    if include == 0:
                        found.pop(field, None)
                return found
        return None

    async def update_one(self, query: dict, update: dict):
        for doc in self._docs:
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    def raw(self, channel_id: str) -> dict | None:
        """Return the stored document itself — lets tests assert on what was persisted."""
        for doc in self._docs:
            if doc.get("channel_id") == channel_id:
                return doc
        return None


class FakeDB:
    def __init__(self, channels: list[dict] | None = None):
        self.channels = FakeCollection(channels)


@pytest.fixture
def fake_db() -> FakeDB:
    """Two channels, neither with a key yet."""
    return FakeDB(
        [
            {"channel_id": "histriphy", "name": "Histriphy", "platform": "youtube"},
            {"channel_id": "otherchan", "name": "Other", "platform": "youtube"},
        ]
    )


@pytest.fixture
def client(fake_db):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    # Constructed without the context manager on purpose: entering it would run
    # the app lifespan, which dials a real MongoDB.
    app.dependency_overrides[get_db] = lambda: fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": GLOBAL_API_KEY}
