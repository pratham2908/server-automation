"""Followers from Instagram's export: reading the file, comparing imports, and the routes."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services import follower_export as fx
from app.timezone import IST
from tests.mongo_fakes import DB

CHANNEL = {"channel_id": "zk", "platform": "instagram", "instagram_username": "zarakapoor._", "subscriber_count": 3}
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=IST)


def _entry(handle: str, ts: int = 1761900000, *, style: str = "value") -> dict[str, Any]:
    item: dict[str, Any] = {"href": f"https://www.instagram.com/{handle}", "timestamp": ts}
    if style == "value":
        item["value"] = handle
    elif style == "href_u":
        item["href"] = f"https://www.instagram.com/_u/{handle}"
    entry: dict[str, Any] = {"title": "", "media_list_data": [], "string_list_data": [item]}
    if style == "title":
        entry["title"] = handle
    return entry


def _zip(files: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data if isinstance(data, str) else json.dumps(data))
    return buf.getvalue()


# --- reading the export -------------------------------------------------------------------------


class TestRead:
    def test_every_export_style_gives_lowercased_handles_and_follow_times(self):
        data = [_entry("Ravi"), _entry("anjali", style="title"), _entry("neha", style="href_u")]
        got = fx.parse_followers_json(data)
        assert set(got) == {"ravi", "anjali", "neha"}
        assert got["ravi"] == datetime.fromtimestamp(1761900000, tz=UTC)

    def test_a_wrapped_list_is_found_too(self):
        assert set(fx.parse_followers_json({"relationships_followers": [_entry("ravi")]})) == {"ravi"}

    def test_a_zip_reads_every_followers_file_and_skips_following(self):
        content = _zip(
            {
                "connections/followers_and_following/followers_1.json": [_entry("ravi")],
                "connections/followers_and_following/followers_2.json": [_entry("anjali")],
                "connections/followers_and_following/following.json": {"relationships_following": [_entry("x")]},
            }
        )
        assert set(fx.read_export("instagram-zarakapoor._-2026-11-03-abc.zip", content)) == {"ravi", "anjali"}

    def test_html_and_junk_get_a_clear_message(self):
        with pytest.raises(fx.ExportError, match="JSON"):
            fx.read_export("x.zip", _zip({"connections/followers_and_following/followers_1.html": "<html>"}))
        with pytest.raises(fx.ExportError, match="Followers and following"):
            fx.read_export("x.zip", _zip({"personal_information/info.json": {}}))
        with pytest.raises(fx.ExportError, match="isn't JSON"):
            fx.read_export("followers_1.json", b"not json")

    def test_the_zip_name_dates_the_export_and_must_be_this_account(self):
        now = datetime(2026, 11, 5, 9, 0, tzinfo=IST)
        taken = fx.export_taken_at("instagram-zarakapoor._-2026-11-03-AbC.zip", CHANNEL, now)
        assert taken == datetime(2026, 11, 3, 23, 59, tzinfo=IST)
        assert fx.export_taken_at("followers_1.json", CHANNEL, now) == now
        with pytest.raises(fx.ExportError, match="@mehrkaul"):
            fx.export_taken_at("instagram-mehrkaul-2026-11-03-x.zip", CHANNEL, now)


# --- comparing imports --------------------------------------------------------------------------


async def _import(db: DB, handles: list[str], at: datetime) -> dict[str, Any]:
    followers = {h: datetime(2026, 10, 30, tzinfo=UTC) for h in handles}
    return await fx.import_snapshot(db, db.channels.docs[0], followers, taken_at=at, source_name="t", now=at)


@pytest.mark.asyncio
class TestImport:
    async def test_first_import_is_the_baseline_then_changes_are_found(self):
        db = DB(channels=[dict(CHANNEL)])
        first = await _import(db, ["ravi", "anjali", "neha"], T0)
        assert (first["first_import"], first["new"], first["unfollowed"]) == (True, 0, 0)

        # Insights show unfollows on exactly one (Pacific, Instagram's) day of the gap: that's the likely day.
        start = datetime.fromisoformat(fx.instagram_day(T0)).date()
        days = [(start + timedelta(days=i)).isoformat() for i in range(4)]
        db.instagram_account_daily.docs.extend(
            {"channel_id": "zk", "date": d, "unfollows": 1 if d == days[2] else 0} for d in days
        )
        second = await _import(db, ["ravi", "neha", "kiran"], T0 + timedelta(days=3))
        assert (second["new"], second["unfollowed"], second["refollowed"]) == (1, 1, 0)
        anjali = next(d for d in db.followers.docs if d["username"] == "anjali")
        assert anjali["status"] == "unfollowed" and anjali["likely_day"] == days[2]
        assert anjali["unfollowed_after"] == T0 and anjali["unfollowed_before"] == T0 + timedelta(days=3)

        third = await _import(db, ["ravi", "neha", "kiran", "anjali"], T0 + timedelta(days=5))
        assert third["refollowed"] == 1
        anjali = next(d for d in db.followers.docs if d["username"] == "anjali")
        assert anjali["status"] == "following" and "likely_day" not in anjali
        kinds = sorted((e["username"], e["kind"]) for e in db.follower_events.docs)
        assert kinds == [("anjali", "refollowed"), ("anjali", "unfollowed"), ("kiran", "followed")]

    async def test_no_likely_day_when_insights_have_gaps_or_several_days(self):
        db = DB(channels=[dict(CHANNEL)])
        await _import(db, ["ravi", "anjali"], T0)
        await _import(db, ["ravi"], T0 + timedelta(days=2))
        assert next(d for d in db.followers.docs if d["username"] == "anjali")["likely_day"] is None

    async def test_an_older_or_empty_export_is_refused(self):
        db = DB(channels=[dict(CHANNEL)])
        await _import(db, ["ravi"], T0)
        with pytest.raises(fx.ExportError, match="older"):
            await _import(db, ["ravi"], T0 - timedelta(days=1))
        with pytest.raises(fx.ExportError, match="empty"):
            await _import(db, [], T0 + timedelta(days=1))

    async def test_handles_are_none_until_an_import(self):
        db = DB(channels=[dict(CHANNEL)])
        assert await fx.follower_handles(db, "zk") is None
        await _import(db, ["ravi"], T0)
        assert await fx.follower_handles(db, "zk") == {"ravi"}


# --- routes -------------------------------------------------------------------------------------


@pytest.fixture
def wired():
    from app import main
    from app.database import get_db
    from app.dependencies import verify_api_key

    db = DB(
        channels=[dict(CHANNEL)],
        comment_log=[
            {"channel_id": "zk", "author": "ravi", "own": False, "media_id": "m1", "commented_at": T0},
            {"channel_id": "zk", "author": "ravi", "own": False, "media_id": "m2", "commented_at": T0},
            {"channel_id": "zk", "author": "stranger", "own": False, "media_id": "m1", "commented_at": T0},
        ],
        dm_conversations=[{"channel_id": "zk", "peer_id": "77777", "peer_username": "Neha", "history": "complete"}],
        dm_messages=[
            {
                "message_id": "d1",
                "channel_id": "zk",
                "peer_id": "77777",
                "from_me": False,
                "text": "hi",
                "created_at": T0,
            }
        ],
    )
    main.app.dependency_overrides[get_db] = lambda: db
    main.app.dependency_overrides[verify_api_key] = lambda: None
    yield TestClient(main.app), db
    main.app.dependency_overrides.pop(get_db, None)
    main.app.dependency_overrides.pop(verify_api_key, None)


def _upload(client: TestClient, handles: list[str], date: str) -> Any:
    content = _zip({"connections/followers_and_following/followers_1.json": [_entry(h) for h in handles]})
    files = {"file": (f"instagram-zarakapoor._-{date}-x.zip", content, "application/zip")}
    return client.post("/api/v1/channels/zk/followers/import", files=files)


def test_import_list_and_summary(wired):
    client, _ = wired
    assert client.get("/api/v1/channels/zk/followers/summary").json()["has_import"] is False
    assert _upload(client, ["ravi", "neha"], "2026-10-30").json()["first_import"] is True
    result = _upload(client, ["ravi", "kiran"], "2026-11-02").json()
    assert (result["new"], result["unfollowed"], result["api_followers"]) == (1, 1, 3)

    rows = client.get("/api/v1/channels/zk/followers", params={"sort": "comments"}).json()["rows"]
    assert [(r["username"], r["comments"]) for r in rows] == [("ravi", 2), ("kiran", 0)]
    gone = client.get("/api/v1/channels/zk/followers", params={"status": "unfollowed"}).json()["rows"]
    assert gone[0]["username"] == "neha" and gone[0]["dmed"] is True and gone[0]["unfollowed_after"]

    summary = client.get("/api/v1/channels/zk/followers/summary").json()
    assert summary["last_import"]["count"] == 2 and summary["last_import"]["imports"] == 2
    assert [p["username"] for p in summary["changes"]["followed"]] == ["kiran"]
    assert [p["username"] for p in summary["changes"]["unfollowed"]] == ["neha"]
    assert summary["commenters_not_following"] == [{"username": "stranger", "comments": 1, "dmed": False}]
    assert summary["followers_who_comment"] == 1 and len(summary["series"]) == 30
    assert summary["days"] == 30 and summary["totals"]["days"] == 30 and summary["reels"] == []
    week = client.get("/api/v1/channels/zk/followers/summary", params={"days": 7}).json()
    assert len(week["series"]) == 7 and week["totals"]["days"] == 7
    assert client.get("/api/v1/channels/zk/followers/summary", params={"days": 3}).status_code == 422


def test_a_wrong_account_or_older_export_is_a_422(wired):
    client, _ = wired
    content = _zip({"connections/followers_and_following/followers_1.json": [_entry("ravi")]})
    files = {"file": ("instagram-mehrkaul-2026-11-03-x.zip", content, "application/zip")}
    assert client.post("/api/v1/channels/zk/followers/import", files=files).status_code == 422
    assert _upload(client, ["ravi"], "2026-11-02").status_code == 200
    assert _upload(client, ["ravi"], "2026-11-01").status_code == 422


def test_commenters_and_dms_say_who_follows(wired):
    client, _ = wired
    top = client.get("/api/v1/channels/zk/commenters", params={"window": "all"}).json()["top"]
    assert all(a["follows_us"] is None for a in top)
    _upload(client, ["ravi", "neha"], "2026-11-02")
    top = {
        a["author"]: a["follows_us"]
        for a in client.get("/api/v1/channels/zk/commenters", params={"window": "all"}).json()["top"]
    }
    assert top == {"ravi": True, "stranger": False}
    body = client.get("/api/v1/channels/zk/instagram/dms").json()
    assert body["conversations"][0]["follows_us"] is True and body["facets"]["follows"] == {"yes": 1}
    assert client.get("/api/v1/channels/zk/instagram/dms", params={"follows": "no"}).json()["total"] == 0
