"""Taps on our buttons and chips: read from the webhook, queued once, answered by the worker."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
import requests

from app.services import comment_dm_worker, dm_outbox, dm_replies
from app.services.dm_content import DmMessage, reply_payload
from app.services.dm_taps import parse_taps
from app.timezone import IST
from tests.mongo_fakes import DB

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=IST)
ME = "17841400000000001"
THEM = "9988776655"
CHANNEL = {"channel_id": "ch", "platform": "instagram", "instagram_user_id": ME, "instagram_username": "tryalgoviz"}
STAMP = int(NOW.timestamp() * 1000)


def _delivery(*items: dict[str, Any]) -> dict[str, Any]:
    return {"object": "instagram", "entry": [{"id": ME, "time": STAMP, "messaging": list(items)}]}


def _postback(payload: str, mid: str = "pb1", sender: str = THEM) -> dict[str, Any]:
    return {
        "sender": {"id": sender},
        "recipient": {"id": ME},
        "timestamp": STAMP,
        "postback": {"mid": mid, "title": "Send it", "payload": payload},
    }


def _chip(payload: str, mid: str = "qr1") -> dict[str, Any]:
    message = {"mid": mid, "text": "Link", "quick_reply": {"payload": payload}}
    return {"sender": {"id": THEM}, "recipient": {"id": ME}, "timestamp": STAMP, "message": message}


class FakeService:
    provider = "instagram"

    def __init__(self, follows: bool | None = True, error: Exception | None = None) -> None:
        self.follows = follows
        self.error = error
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.private: list[dict[str, Any]] = []

    def send_message(self, ig_user_id: str, recipient_id: str, message: dict[str, Any]) -> str:
        if self.error:
            raise self.error
        self.sent.append((recipient_id, message))
        return f"m{len(self.sent)}"

    def messaging_profile(self, igsid: str) -> dict[str, Any]:
        if self.follows is None:
            raise requests.HTTPError("profile unavailable")
        return {"username": "fan", "is_user_follow_business": self.follows}

    def send_private_reply(self, ig_user_id: str, comment_id: str, message: dict[str, Any]) -> str:
        self.private.append(message)
        return "pm1"

    def reply_to_comment(self, comment_id: str, message: str) -> str:
        return "r1"


class FakeManager:
    def __init__(self, service: FakeService) -> None:
        self.service = service

    async def get_service_and_user_id(self, channel_id: str, prefer: str | None = None):
        return self.service, ME


REPLY = DmMessage(text="Here's the link", buttons=[{"title": "Open", "url": "https://tryalgoviz.com"}])
GATED = DmMessage(text="Here's the link", followers_only_text="Follow us first, then tap below")


async def _db(*replies: DmMessage) -> DB:
    db = DB(channels=[dict(CHANNEL)])
    await dm_replies.register(db, "ch", list(replies), NOW)
    return db


def test_a_postback_and_a_chip_are_both_taps_and_echoes_are_not() -> None:
    echo = {**_chip("dmr:x", mid="e1"), "message": {"mid": "e1", "is_echo": True, "quick_reply": {"payload": "dmr:x"}}}
    plain = {"sender": {"id": THEM}, "timestamp": STAMP, "message": {"mid": "t1", "text": "hello"}}
    taps = parse_taps(_delivery(_postback("dmr:abc"), _chip("dmr:def"), echo, plain))
    assert [(t.kind, t.payload, t.tap_id) for t in taps] == [
        ("postback", "dmr:abc", "pb1"),
        ("quick_reply", "dmr:def", "qr1"),
    ]


def test_an_ice_breaker_tap_without_a_mid_still_dedupes() -> None:
    item = _postback("dmr:abc")
    del item["postback"]["mid"]
    [tap] = parse_taps(_delivery(item))
    assert tap.tap_id == f"{THEM}:dmr:abc:{STAMP}"


@pytest.mark.asyncio
class TestTaps:
    async def test_a_tap_queues_its_reply_once_however_often_meta_delivers_it(self) -> None:
        db = await _db(REPLY)
        delivery = _delivery(_postback(reply_payload("ch", REPLY)))
        assert await dm_outbox.record_webhook(db, delivery, NOW) == 1
        assert await dm_outbox.record_webhook(db, delivery, NOW) == 0
        [row] = db.dm_outbox.docs
        assert (row["recipient_id"], row["status"], row["check_follow"]) == (THEM, "pending", False)
        # The tap shows in the inbox as their side of the chat.
        [kept] = db.dm_messages.docs
        assert (kept["kind"], kept["text"], kept["from_me"]) == ("tap", "Send it", False)

    async def test_a_chip_is_not_kept_twice_since_the_inbox_has_it_as_a_message(self) -> None:
        db = await _db(REPLY)
        await dm_outbox.record_webhook(db, _delivery(_chip(reply_payload("ch", REPLY))), NOW)
        assert len(db.dm_outbox.docs) == 1 and db.dm_messages.docs == []

    async def test_payloads_that_are_not_ours_or_unknown_send_nothing(self) -> None:
        db = await _db(REPLY)
        await dm_outbox.record_webhook(db, _delivery(_chip("qr:More"), _postback("dmr:deadbeef", mid="pb2")), NOW)
        assert db.dm_outbox.docs == []

    async def test_our_own_tap_never_answers_itself(self) -> None:
        db = await _db(REPLY)
        await dm_outbox.record_webhook(db, _delivery(_postback(reply_payload("ch", REPLY), sender=ME)), NOW)
        assert db.dm_outbox.docs == []


async def _queued(db: DB, reply: DmMessage) -> dict[str, Any]:
    await dm_outbox.record_webhook(db, _delivery(_postback(reply_payload("ch", reply))), NOW)
    item = await dm_outbox.claim_due(db, NOW)
    assert item is not None
    return item


@pytest.mark.asyncio
class TestSend:
    async def test_the_reply_goes_to_whoever_tapped_and_is_kept(self) -> None:
        db = await _db(REPLY)
        service = FakeService()
        assert await dm_outbox.send_one(db, FakeManager(service), await _queued(db, REPLY), NOW) == "sent"
        [(to, message)] = service.sent
        assert to == THEM and message["attachment"]["payload"]["template_type"] == "button"
        assert db.dm_outbox.docs[0]["status"] == "sent"
        assert any(m["from_me"] and m["text"] == "Here's the link" for m in db.dm_messages.docs)

    async def test_a_non_follower_gets_the_gate_with_a_check_again_button(self) -> None:
        db = await _db(GATED)
        service = FakeService(follows=False)
        await dm_outbox.send_one(db, FakeManager(service), await _queued(db, GATED), NOW)
        [(_, message)] = service.sent
        payload = message["attachment"]["payload"]
        assert payload["text"] == "Follow us first, then tap below"
        assert payload["buttons"][1]["payload"] == reply_payload("ch", GATED)
        assert db.dm_outbox.docs[0]["gated"] is True and db.dm_outbox.docs[0]["follows"] is False

    async def test_check_again_after_following_sends_the_reply(self) -> None:
        db = await _db(GATED)
        service = FakeService(follows=True)
        await dm_outbox.send_one(db, FakeManager(service), await _queued(db, GATED), NOW)
        assert service.sent[0][1] == {"text": "Here's the link"}

    async def test_when_instagram_wont_say_they_get_the_reply(self) -> None:
        db = await _db(GATED)
        service = FakeService(follows=None)
        await dm_outbox.send_one(db, FakeManager(service), await _queued(db, GATED), NOW)
        assert service.sent[0][1] == {"text": "Here's the link"} and db.dm_outbox.docs[0]["follows"] is None

    async def test_a_refusal_is_recorded_and_not_retried(self) -> None:
        db = await _db(REPLY)
        service = FakeService(error=requests.HTTPError("nope"))
        assert await dm_outbox.send_one(db, FakeManager(service), await _queued(db, REPLY), NOW) == "failed"
        assert db.dm_outbox.docs[0]["error"] == "nope"

    async def test_a_send_cut_off_by_a_restart_is_failed_not_resent(self) -> None:
        db = await _db(REPLY)
        await _queued(db, REPLY)
        assert await dm_outbox.fail_interrupted(db, NOW + timedelta(minutes=11)) == 1


@pytest.mark.asyncio
async def test_a_comment_dm_reply_button_answers_its_tap_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Comment → private reply with a reply button → they tap it → the worker sends the reply."""
    db = DB(channels=[dict(CHANNEL)])
    service = FakeService()
    button = {"type": "reply", "title": "Send it", "reply": REPLY.model_dump(mode="json")}
    event = {"comment_id": "c1", "channel_id": "ch", "dm_text": "Tap for the link", "buttons": [button], "attempts": 0}
    db.comment_dm_events.docs.append({**event, "status": "sending"})
    assert await comment_dm_worker.send_one(db, FakeManager(service), event, NOW) == "sent"
    [private] = service.private
    postback = private["attachment"]["payload"]["buttons"][0]
    assert postback["type"] == "postback"
    # The DM's reply was registered as it went, so the tap a moment later finds it.
    await dm_outbox.record_webhook(db, _delivery(_postback(postback["payload"])), NOW)
    monkeypatch.setattr(comment_dm_worker, "now_ist", lambda: NOW)
    await dm_outbox.send_due(db, FakeManager(service), lambda: NOW)
    assert service.sent[0][1]["attachment"]["payload"]["text"] == "Here's the link"


@pytest.mark.asyncio
async def test_a_refused_carousel_on_a_private_reply_goes_as_text() -> None:
    db = DB(channels=[dict(CHANNEL)])

    class Refusing(FakeService):
        def send_private_reply(self, ig_user_id: str, comment_id: str, message: dict[str, Any]) -> str:
            if "attachment" in message:
                response = requests.Response()
                response.status_code = 400
                response._content = b'{"error": {"message": "template not allowed", "code": 100}}'
                raise requests.HTTPError("400", response=response)
            self.private.append(message)
            return "pm1"

    service = Refusing()
    cards = [{"title": "Graphs", "subtitle": "BFS", "link": "https://tryalgoviz.com/bfs"}]
    event = {"comment_id": "c1", "channel_id": "ch", "dm_text": "", "cards": cards, "attempts": 0}
    db.comment_dm_events.docs.append({**event, "status": "sending"})
    assert await comment_dm_worker.send_one(db, FakeManager(service), event, NOW) == "sent"
    assert service.private == [{"text": "Graphs\nBFS\nhttps://tryalgoviz.com/bfs"}]
    assert db.comment_dm_events.docs[0]["dm_format"] == "text_fallback"
