"""Story kit routes against an in-memory database and a fake bucket."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.routers import story_kit as routes
from tests.conftest import GLOBAL_API_KEY


def _matches(doc, q):
    for key, want in q.items():
        have = doc.get(key)
        if isinstance(want, dict):
            if "$exists" in want and (key in doc) != want["$exists"]:
                return False
            if "$nin" in want and have in want["$nin"]:
                return False
            if "$gte" in want and (have is None or have.replace(tzinfo=timezone.utc) < want["$gte"]):
                return False
        elif have != want:
            return False
    return True


class Cursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return list(self.docs)


class Coll:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    async def find_one(self, q, projection=None):
        return next((d for d in self.docs if _matches(d, q)), None)

    def find(self, q, projection=None):
        return Cursor([d for d in self.docs if _matches(d, q)])

    async def update_one(self, q, u):
        d = await self.find_one(q)
        if d:
            d.update(u.get("$set", {}))
            for key in u.get("$unset", {}):
                d.pop(key, None)

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


# 09:30 IST today (naive UTC, as Mongo returns it): always inside the page's window, whatever day the tests run.
REEL_AT = datetime.now(timezone.utc).replace(hour=4, minute=0, second=0, microsecond=0, tzinfo=None)


def _video(vid, trial=None):
    return {
        "_id": vid,
        "channel_id": "TryAlgoViz",
        "video_id": vid,
        "title": f"Reel {vid}",
        "r2_object_key": f"v/{vid}.mp4",
        "thumbnail_r2_key": f"t/{vid}.jpg",
        "scheduled_at": REEL_AT,
        "trial_graduation": trial,
    }


@pytest.fixture
def world(monkeypatch):
    db = SimpleNamespace(
        videos=Coll([_video("a"), _video("b", "MANUAL")]),
        story_kits=Coll(),
        channels=Coll([{"channel_id": "TryAlgoViz"}]),
    )
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
    return world.client.post(
        "/api/v1/channels/TryAlgoViz/story-kit", json={"reels": reels}, headers={"X-API-Key": GLOBAL_API_KEY}
    )


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
    assert world.client.get(f"/s/{token}/b/video").status_code == 404  # an ungraduated trial is not on the page
    assert world.client.get("/s/not-a-token").status_code == 404


def test_every_run_lands_on_the_same_page_which_keeps_earlier_reels(world):
    world.db.videos.docs.append(_video("c"))
    first = _post(world, [{"video_id": "a", "slug": "alpha"}]).json()["kit"]
    second = _post(world, [{"video_id": "c", "slug": "gamma"}]).json()["kit"]
    assert first == second
    page = world.client.get(f"/s/{first.rsplit('/', 1)[1]}").text
    assert "alpha" in page and "gamma" in page


def test_scheduled_is_saved_on_the_server_and_can_be_undone(world):
    token = _post(world, [{"video_id": "a", "slug": "alpha"}]).json()["kit"].rsplit("/", 1)[1]
    assert world.client.post(f"/s/{token}/a/done", json={"done": True}).json() == {"done": True}
    assert "Scheduled (1)" in world.client.get(f"/s/{token}").text
    world.client.post(f"/s/{token}/a/done", json={"done": False})
    assert "story_scheduled_at" not in world.db.videos.docs[0]
    assert world.client.post(f"/s/{token}/b/done", json={"done": True}).status_code == 404


def test_an_old_per_run_link_redirects_while_it_would_have_worked(world):
    token = _post(world, [{"video_id": "a", "slug": "alpha"}]).json()["kit"].rsplit("/", 1)[1]
    now = datetime.now(timezone.utc)
    world.db.story_kits.docs.append(
        {"token": "old-live", "channel_id": "TryAlgoViz", "expires_at": now + timedelta(days=1)}
    )
    world.db.story_kits.docs.append(
        {"token": "old-dead", "channel_id": "TryAlgoViz", "expires_at": now - timedelta(minutes=1)}
    )
    moved = world.client.get("/s/old-live", follow_redirects=False)
    assert moved.status_code == 303 and moved.headers["location"].endswith(f"/s/{token}")
    assert world.client.get("/s/old-dead").status_code == 404
