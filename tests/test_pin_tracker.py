from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import pin_email as pe
from app.services import pin_tracker as pt
from app.timezone import IST

POSTED = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


def video(i, status=None, channel="ch", posted=POSTED, **extra):
    doc = {
        "video_id": f"v{i}",
        "channel_id": channel,
        "title": f"Reel {i}",
        "instagram_media_id": f"m{i}",
        "first_comment": "Comment MEET and I'll DM you",
        "first_comment_status": "posted",
        "first_comment_id": f"c{i}",
        "first_comment_posted_at": posted,
        **extra,
    }
    if status:
        doc["pin_status"] = status
    return doc


def test_pin_item_links_straight_to_our_comment():
    item = pt.pin_item(video(1), "https://www.instagram.com/reel/AbC123/")
    assert item["comment_url"] == "https://www.instagram.com/reel/AbC123/c/c1/"
    assert item["reel_url"] == "https://www.instagram.com/reel/AbC123/" and item["status"] is None
    assert item["posted_at"].startswith("2026-11-10T14:30")


def test_pin_item_without_a_permalink_has_no_links():
    item = pt.pin_item(video(1), None)
    assert item["comment_url"] is None and item["reel_url"] is None


def test_tracking_starts_on_10_november_ist():
    assert pt.PIN_TRACKING_SINCE == datetime(2026, 11, 10, tzinfo=IST)
    assert pt.tracked_filter()["first_comment_status"] == "posted"


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


class _Videos:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query):
        since = query["first_comment_posted_at"]["$gte"]
        return _Cursor(
            [d for d in self.docs if d["first_comment_status"] == "posted" and d["first_comment_posted_at"] >= since]
        )

    async def update_one(self, flt, update):
        for d in self.docs:
            if (
                d["video_id"] == flt["video_id"]
                and d["first_comment_posted_at"] >= flt["first_comment_posted_at"]["$gte"]
            ):
                if "$set" in update:
                    d.update(update["$set"])
                for k in update.get("$unset", {}):
                    d.pop(k, None)
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)


class _Channels:
    async def find_one(self, q):
        return {"channel_id": q["channel_id"], "name": f"Name {q['channel_id']}", "handle": "@h"}


@pytest.fixture
def stub_graph(monkeypatch):
    async def fake_graph(db, manager, channel_id):
        return None

    async def fake_links(db, graph, ids):
        return {i: f"https://www.instagram.com/reel/{i.upper()}/" for i in ids}

    monkeypatch.setattr(pt, "graph_for", fake_graph)
    monkeypatch.setattr(pt, "permalinks", fake_links)


@pytest.mark.asyncio
async def test_overview_splits_pending_from_finished_and_ignores_older_comments(stub_graph):
    old = POSTED - timedelta(days=3)
    db = SimpleNamespace(
        channels=_Channels(),
        videos=_Videos([video(1), video(2, "done"), video(3, "skipped"), video(4, posted=old), video(5, channel="b")]),
    )
    out = await pt.pins_overview(db, None)
    by = {b["channel_id"]: b for b in out["channels"]}
    assert [i["video_id"] for i in by["ch"]["pending"]] == ["v1"]
    assert {i["video_id"] for i in by["ch"]["finished"]} == {"v2", "v3"} and by["ch"]["finished_count"] == 2
    assert out["pending_total"] == 2


@pytest.mark.asyncio
async def test_ticking_and_unticking(stub_graph):
    docs = [video(1)]
    db = SimpleNamespace(videos=_Videos(docs))
    assert await pt.set_pin_status(db, "v1", "done") is True
    assert docs[0]["pin_status"] == "done"
    assert await pt.set_pin_status(db, "v1", None) is True
    assert "pin_status" not in docs[0]
    assert await pt.set_pin_status(db, "nope", "done") is False


# ---- the email -------------------------------------------------------------------------


def overview(*ids):
    items = [pt.pin_item(video(i), f"https://www.instagram.com/reel/R{i}/") for i in ids]
    return {
        "channels": [{"channel_id": "ch", "name": "TryAlgoViz <x>", "handle": "@t", "pending": items}],
        "pending_total": len(items),
    }


def test_email_links_every_pending_reel_and_the_page():
    mail = pe.format_pins_email(overview(1, 2), "https://app.test/")
    assert mail.subject == "2 comments waiting to be pinned"
    assert "https://app.test/?hub=production&amp;view=pins" in mail.html
    for i in (1, 2):
        assert f"/reel/R{i}/c/c{i}/" in mail.html and f"/reel/R{i}/c/c{i}/" in mail.text
    assert "TryAlgoViz &lt;x&gt;" in mail.html and "<x>" not in mail.html


class _Config:
    def __init__(self, doc=None):
        self.doc = doc

    async def find_one(self, q):
        return self.doc

    async def update_one(self, q, update, upsert=False):
        self.doc = {**(self.doc or {}), **update["$set"]}


@pytest.fixture
def mail_env(monkeypatch):
    sent = []
    state = {"ok": True, "ov": overview(1)}

    async def fake_overview(db, manager):
        return state["ov"]

    async def fake_send(settings, recipient, subject, text, html):
        sent.append(subject)
        return state["ok"]

    async def fake_owner(db, settings):
        return "o@example.com"

    monkeypatch.setattr(pe, "pins_overview", fake_overview)
    monkeypatch.setattr(pe, "send_email", fake_send)
    monkeypatch.setattr(pe, "resolve_owner_email", fake_owner)
    return sent, state


SETTINGS = SimpleNamespace(ANALYZER_PUBLIC_URL="https://app.test")


@pytest.mark.asyncio
async def test_sends_once_for_new_pending_then_waits_for_something_new(mail_env):
    sent, state = mail_env
    db = SimpleNamespace(config=_Config())
    assert await pe.send_pending_pins_email(db, None, SETTINGS) == "sent"
    assert await pe.send_pending_pins_email(db, None, SETTINGS) == "nothing new"
    state["ov"] = overview(1, 2)
    assert await pe.send_pending_pins_email(db, None, SETTINGS) == "too soon"
    db.config.doc["last_sent_at"] = datetime.now(timezone.utc) - timedelta(hours=4)
    assert await pe.send_pending_pins_email(db, None, SETTINGS) == "sent"
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_nothing_pending_sends_nothing_and_force_overrides(mail_env):
    sent, state = mail_env
    db = SimpleNamespace(config=_Config())
    state["ov"] = {"channels": [], "pending_total": 0}
    assert await pe.send_pending_pins_email(db, None, SETTINGS, force=True) == "nothing new"
    state["ov"] = overview(1)
    await pe.send_pending_pins_email(db, None, SETTINGS)
    assert await pe.send_pending_pins_email(db, None, SETTINGS, force=True) == "sent"
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_a_failed_send_is_not_recorded_so_it_retries(mail_env):
    sent, state = mail_env
    state["ok"] = False
    db = SimpleNamespace(config=_Config())
    assert await pe.send_pending_pins_email(db, None, SETTINGS) == "not sent"
    state["ok"] = True
    assert await pe.send_pending_pins_email(db, None, SETTINGS) == "sent"
