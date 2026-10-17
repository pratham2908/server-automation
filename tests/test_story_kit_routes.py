"""Story kit routes against an in-memory database and a fake bucket."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.routers import story_kit as routes
from tests.conftest import GLOBAL_API_KEY


class Coll:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    async def find_one(self, q, projection=None):
        return next((d for d in self.docs if all(d.get(k) == v for k, v in q.items())), None)

    async def update_one(self, q, u):
        d = await self.find_one(q)
        if d:
            d.update(u.get("$set", {}))

    async def insert_one(self, doc):
        self.docs.append(doc)


class FakeBody:
    def iter_chunks(self, chunk_size):
        yield b"mp4-bytes"


class FakeClient:
    def get_object(self, Bucket, Key):
        return {"ContentLength": 9, "Body": FakeBody()}


class FakeR2:
    _bucket = "b"
    _client = FakeClient()

    def generate_presigned_url(self, key, expires_in=3600):
        return f"https://r2/{key}?exp={expires_in}"


def _video(vid, trial=None):
    return {"_id": vid, "channel_id": "TryAlgoViz", "video_id": vid, "title": f"Reel {vid}", "r2_object_key": f"v/{vid}.mp4",
            "thumbnail_r2_key": f"t/{vid}.jpg", "scheduled_at": datetime(2026, 10, 17, 4, 0), "trial_graduation": trial}


@pytest.fixture
def world(monkeypatch):
    db = SimpleNamespace(videos=Coll([_video("a"), _video("b", "MANUAL")]), story_kits=Coll())
    app = FastAPI()
    app.include_router(routes.api_router)
    app.include_router(routes.page_router)
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(routes, "_r2", FakeR2)
    sent = []

    async def fake_send(settings, to, subject, text, html=None):
        sent.append((subject, text))
        return True

    async def fake_owner(db, settings):
        return "owner@x"

    monkeypatch.setattr(routes, "send_email", fake_send)
    monkeypatch.setattr(routes, "resolve_owner_email", fake_owner)
    return SimpleNamespace(db=db, client=TestClient(app), sent=sent)


def _post(world, reels):
    return world.client.post("/api/v1/channels/TryAlgoViz/story-kit", json={"reels": reels}, headers={"X-API-Key": GLOBAL_API_KEY})


def test_kit_emails_normal_posts_only_and_remembers_the_trial_slug(world):
    res = _post(world, [{"video_id": "a", "slug": "alpha"}, {"video_id": "b", "slug": "beta"}]).json()
    assert res["emailed"] is True and res["trials_without_story"] == ["b"]
    assert [s["video_id"] for s in res["stories"]] == ["a"]
    assert world.sent[0][0] == "Stories to schedule: 1 reel" and "9:37 AM" in world.sent[0][1]
    assert world.db.videos.docs[1]["story_slug"] == "beta"  # saved for the graduation email


def test_kit_needs_the_api_key_and_a_scheduled_video(world):
    assert world.client.post("/api/v1/channels/TryAlgoViz/story-kit", json={"reels": []}).status_code in (401, 422)
    assert _post(world, [{"video_id": "nope", "slug": "x"}]).status_code == 404


def test_page_and_video_open_with_the_token_and_not_without(world):
    url = _post(world, [{"video_id": "a", "slug": "alpha"}]).json()["kit"]
    token = url.rsplit("/", 1)[1]
    page = world.client.get(f"/s/{token}")
    assert page.status_code == 200 and "alpha" in page.text and "no-store" in page.headers["cache-control"]
    video = world.client.get(f"/s/{token}/a/video")
    assert video.status_code == 200 and video.content == b"mp4-bytes" and video.headers["content-type"] == "video/mp4"
    assert world.client.get(f"/s/{token}/b/video").status_code == 404  # not in this kit
    assert world.client.get("/s/not-a-token").status_code == 404


def test_an_expired_kit_is_gone(world):
    url = _post(world, [{"video_id": "a", "slug": "alpha"}]).json()["kit"]
    token = url.rsplit("/", 1)[1]
    world.db.story_kits.docs[0]["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert world.client.get(f"/s/{token}").status_code == 404
