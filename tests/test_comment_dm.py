"""Comment-to-DM: the pure rules, the queue, the sender and the webhook route."""

from __future__ import annotations

import hashlib
import hmac
import json
import random
from datetime import datetime, timedelta
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.services import comment_dm_store as store
from app.services.comment_dm import (
    ANY_COMMENT,
    HOURLY_CAP,
    MAX_ATTEMPTS,
    SKIP_ALREADY_MESSAGED,
    SKIP_BEFORE_RULE,
    SKIP_OWN,
    SKIP_TOO_OLD,
    CommentEvent,
    DmRule,
    StoredRule,
    effective_rule,
    matched_trigger,
    parse_comment_webhook,
    retry_at,
    send_delay,
    skip_reason,
    stored_rule,
    verify_signature,
)
from app.services.comment_dm_worker import poll_channel, send_one
from app.timezone import IST
from tests.mongo_fakes import DB

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=IST)


def _rule(**kw: Any) -> DmRule:
    return DmRule(**{"dm_text": "Here's the link!", **kw})


def _event(**kw: Any) -> CommentEvent:
    base = {"comment_id": "c1", "media_id": "111111", "text": "link please", "username": "anjali", "from_id": "u1"}
    return CommentEvent(**{**base, **kw})


# --- rules -----------------------------------------------------------------------------------


class TestDmRule:
    def test_triggers_are_trimmed_lowercased_and_deduplicated(self):
        assert _rule(triggers=[" Link ", "link", "", "PDF"]).triggers == ["link", "pdf"]

    def test_blank_dm_is_refused(self):
        with pytest.raises(ValidationError):
            _rule(dm_text="   ")

    def test_an_overlong_public_reply_is_refused(self):
        with pytest.raises(ValidationError):
            _rule(reply_texts=["x" * 301])

    def test_blank_replies_are_dropped(self):
        assert _rule(reply_texts=["Follow for more!", "  "]).reply_texts == ["Follow for more!"]


class TestButtons:
    def test_at_most_three_with_short_labels_and_real_urls(self):
        with pytest.raises(ValidationError):
            _rule(buttons=[BUTTON] * 4)
        with pytest.raises(ValidationError):
            _rule(buttons=[{"title": "x" * 21, "url": "https://a.com"}])
        with pytest.raises(ValidationError):
            _rule(buttons=[{"title": "Go", "url": "javascript:alert(1)"}])

    def test_the_template_text_limit_applies_only_with_buttons(self):
        _rule(dm_text="x" * 900)
        with pytest.raises(ValidationError):
            _rule(dm_text="x" * 900, buttons=[BUTTON])


class TestMatchedTrigger:
    def test_no_triggers_means_every_comment(self):
        assert matched_trigger(_rule(), "") == ANY_COMMENT

    def test_whole_words_only(self):
        rule = _rule(triggers=["link"])
        assert matched_trigger(rule, "LINK pls") == "link"
        assert matched_trigger(rule, "I linked it") is None

    def test_phrases_and_emoji(self):
        rule = _rule(triggers=["send code", "🔥"])
        assert matched_trigger(rule, "can you send code?") == "send code"
        assert matched_trigger(rule, "wow🔥🔥") == "🔥"

    def test_no_text_never_matches_a_trigger(self):
        assert matched_trigger(_rule(triggers=["link"]), "") is None


class TestEffectiveRule:
    def test_a_reel_rule_switched_off_silences_the_default(self):
        default = StoredRule(rule=_rule())
        assert effective_rule(StoredRule(rule=_rule(enabled=False)), default) is None

    def test_the_default_covers_a_reel_without_its_own_rule(self):
        default = StoredRule(rule=_rule())
        assert effective_rule(None, default) is default

    def test_a_default_switched_off_covers_nothing(self):
        assert effective_rule(None, StoredRule(rule=_rule(enabled=False))) is None


class TestStoredRule:
    def test_switching_on_stamps_now_and_resaving_keeps_it(self):
        first = stored_rule(_rule(), None, NOW)
        assert first.enabled_at == NOW
        later = stored_rule(_rule(dm_text="edited"), first, NOW + timedelta(hours=1))
        assert later.enabled_at == NOW

    def test_off_then_on_again_restamps(self):
        off = stored_rule(_rule(enabled=False), stored_rule(_rule(), None, NOW), NOW + timedelta(hours=1))
        assert off.enabled_at is None
        assert stored_rule(_rule(), off, NOW + timedelta(hours=2)).enabled_at == NOW + timedelta(hours=2)


def test_a_rule_read_back_from_mongo_compares_with_aware_comment_times():
    # Mongo returns naive UTC datetimes; the webhook stamps comments in IST.
    stored = StoredRule.model_validate({"rule": _rule().model_dump(), "enabled_at": datetime(2026, 10, 25, 6, 0)})
    assert stored.enabled_at is not None and stored.enabled_at < NOW


class TestSkipReason:
    def _reason(self, event: CommentEvent, **kw: Any) -> str | None:
        args: dict[str, Any] = {
            "own_ids": {"me"},
            "own_username": "tryalgoviz",
            "active_since": NOW - timedelta(days=1),
            "now": NOW,
            "already_messaged": False,
        }
        return skip_reason(event, **{**args, **kw})

    def test_a_fresh_comment_from_someone_else_goes_through(self):
        assert self._reason(_event(created_at=NOW)) is None

    def test_our_own_comments_by_id_or_username(self):
        assert self._reason(_event(from_id="me")) == SKIP_OWN
        assert self._reason(_event(username="TryAlgoViz", from_id="")) == SKIP_OWN

    def test_comments_from_before_the_rule_was_switched_on(self):
        assert self._reason(_event(created_at=NOW - timedelta(days=2))) == SKIP_BEFORE_RULE

    def test_comments_too_old_for_a_private_reply(self):
        assert self._reason(_event(created_at=NOW - timedelta(days=7)), active_since=None) == SKIP_TOO_OLD

    def test_one_dm_per_person_per_reel(self):
        assert self._reason(_event(created_at=NOW), already_messaged=True) == SKIP_ALREADY_MESSAGED


def test_send_delay_is_jittered_around_the_setting():
    rng = random.Random(1)
    delays = [send_delay(_rule(delay_seconds=60), rng).total_seconds() for _ in range(50)]
    assert all(45 <= d <= 90 for d in delays)
    assert send_delay(_rule(delay_seconds=0), rng) == timedelta(0)


def test_retries_stop_after_the_last_attempt():
    assert retry_at(1, NOW) == NOW + timedelta(minutes=5)
    assert retry_at(MAX_ATTEMPTS, NOW) is None


class TestSignature:
    def test_matches_metas_hmac(self):
        body = b'{"object":"instagram"}'
        sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        assert verify_signature("secret", body, sig)

    def test_wrong_or_missing_signature_fails(self):
        assert not verify_signature("secret", b"{}", "sha256=00")
        assert not verify_signature("secret", b"{}", None)


def _comment_payload(at: datetime = NOW, **value: Any) -> dict[str, Any]:
    base = {"id": "c9", "text": "link", "from": {"id": "u9", "username": "ravi"}, "media": {"id": "111111"}}
    change = {"field": "comments", "value": {**base, **value}}
    return {"object": "instagram", "entry": [{"id": "1784", "time": int(at.timestamp()), "changes": [change]}]}


class TestParseWebhook:
    def test_reads_a_comment(self):
        [event] = parse_comment_webhook(_comment_payload())
        assert (event.comment_id, event.media_id, event.account_id, event.username) == ("c9", "111111", "1784", "ravi")
        assert event.created_at == NOW

    def test_ignores_other_fields_and_other_objects(self):
        payload = _comment_payload()
        payload["entry"][0]["changes"][0]["field"] = "messages"
        assert parse_comment_webhook(payload) == []
        assert parse_comment_webhook({"object": "page", "entry": []}) == []

    def test_a_messaging_delivery_with_a_millisecond_time_is_ignored_not_fatal(self):
        payload = {
            "object": "instagram",
            "entry": [{"id": "1784", "time": 1792878917929, "messaging": [{"sender": {"id": "1"}, "read": {}}]}],
        }
        assert parse_comment_webhook(payload) == []

    def test_a_millisecond_time_on_a_comment_is_read_as_milliseconds(self):
        payload = _comment_payload()
        payload["entry"][0]["time"] = int(NOW.timestamp() * 1000)
        [event] = parse_comment_webhook(payload)
        assert event.created_at == NOW

    def test_skips_a_change_without_ids(self):
        assert parse_comment_webhook(_comment_payload(media={})) == []


# --- queue -----------------------------------------------------------------------------------

CHANNEL = {"channel_id": "ch", "platform": "instagram", "instagram_user_id": "me", "instagram_username": "tryalgoviz"}


def _db_with_rule(rule: DmRule, *, default: bool = True) -> DB:
    stored = stored_rule(rule, None, NOW - timedelta(hours=1)).model_dump()
    channel = {**CHANNEL, store.DEFAULT_FIELD: stored} if default else dict(CHANNEL)
    rules = [] if default else [{"channel_id": "ch", "media_id": "111111", **stored}]
    return DB(channels=[channel], comment_dm_rules=rules)


async def _enqueue(db: DB, event: CommentEvent) -> str:
    return await store.enqueue(db, CHANNEL, event, source="webhook", now=NOW, rng=random.Random(0))


@pytest.mark.asyncio
class TestEnqueue:
    async def test_a_matching_comment_is_queued_once(self):
        db = _db_with_rule(_rule(triggers=["link"], reply_texts=["Sent! Follow for more"]))
        assert await _enqueue(db, _event(created_at=NOW)) == store.QUEUED
        assert await _enqueue(db, _event(created_at=NOW)) == store.DUPLICATE
        [doc] = db.comment_dm_events.docs
        assert doc["status"] == "pending" and doc["reply_text"] == "Sent! Follow for more"
        assert doc["send_after"] > NOW

    async def test_no_match_and_no_rule_store_nothing(self):
        db = _db_with_rule(_rule(triggers=["pdf"]))
        assert await _enqueue(db, _event()) == store.NO_MATCH
        assert await _enqueue(DB(channels=[CHANNEL]), _event()) == store.NO_RULE
        assert db.comment_dm_events.docs == []

    async def test_a_reel_rule_works_without_a_default(self):
        db = _db_with_rule(_rule(), default=False)
        assert await _enqueue(db, _event(created_at=NOW)) == store.QUEUED

    async def test_own_and_pre_rule_comments_are_dropped_silently(self):
        db = _db_with_rule(_rule())
        assert await _enqueue(db, _event(from_id="me")) == SKIP_OWN
        assert await _enqueue(db, _event(comment_id="c2", created_at=NOW - timedelta(days=1))) == SKIP_BEFORE_RULE
        assert db.comment_dm_events.docs == []

    async def test_a_second_comment_by_the_same_person_is_logged_as_skipped(self):
        db = _db_with_rule(_rule())
        await _enqueue(db, _event(created_at=NOW))
        assert await _enqueue(db, _event(comment_id="c2", created_at=NOW)) == SKIP_ALREADY_MESSAGED
        assert [d["status"] for d in db.comment_dm_events.docs] == ["pending", "skipped"]

    async def test_the_rule_buttons_travel_with_the_queued_dm(self):
        db = _db_with_rule(_rule(buttons=[BUTTON]))
        await _enqueue(db, _event(created_at=NOW))
        assert db.comment_dm_events.docs[0]["buttons"] == [BUTTON]

    async def test_summary_counts_each_outcome(self):
        def ev(i: int, **kw: Any) -> dict[str, Any]:
            return {"comment_id": f"c{i}", "channel_id": "ch", "created_at": NOW, **kw}

        db = DB(
            comment_dm_events=[
                ev(1, status="sent", reply_comment_id="r1"),
                ev(2, status="sent"),
                ev(3, status="failed"),
                ev(4, status="pending"),
                ev(5, status="sent", created_at=NOW - timedelta(days=3)),
            ]
        )
        recent = await store.summary(db, "ch", NOW - timedelta(hours=24))
        assert recent == {"comments": 4, "dms_sent": 2, "replies_posted": 1, "queued": 1, "failed": 1, "skipped": 0}
        assert (await store.summary(db, "ch", None))["dms_sent"] == 3

    async def test_claim_takes_only_due_pending_events(self):
        db = DB(
            comment_dm_events=[
                {"comment_id": "late", "status": "pending", "send_after": NOW + timedelta(minutes=1)},
                {"comment_id": "due", "status": "pending", "send_after": NOW - timedelta(seconds=5)},
            ]
        )
        claimed = await store.claim_due(db, NOW)
        assert claimed is not None and claimed["comment_id"] == "due" and claimed["status"] == "sending"
        assert await store.claim_due(db, NOW) is None

    async def test_a_send_cut_off_by_a_restart_is_failed_not_retried(self):
        db = DB(comment_dm_events=[{"comment_id": "c", "status": "sending", "claimed_at": NOW - timedelta(hours=1)}])
        assert await store.fail_interrupted(db, NOW - timedelta(minutes=10), NOW) == 1
        assert db.comment_dm_events.docs[0]["status"] == "failed"

    async def test_webhook_channel_by_account_id_then_by_reel(self):
        db = DB(
            channels=[{**CHANNEL, store.WEBHOOK_ID_FIELD: "1784"}, {"channel_id": "other", "platform": "instagram"}],
            videos=[{"channel_id": "other", "instagram_media_id": "222222"}],
        )
        by_account = await store.channel_for_webhook(db, "1784", "999")
        by_reel = await store.channel_for_webhook(db, "unknown", "222222")
        assert by_account is not None and by_account["channel_id"] == "ch"
        assert by_reel is not None and by_reel["channel_id"] == "other"


# --- sender ----------------------------------------------------------------------------------


def _graph_error(code: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = 400
    response._content = json.dumps({"error": {"message": "nope", "code": code}}).encode()
    return requests.HTTPError(response=response)


class FakeService:
    def __init__(
        self, provider: str = "instagram", dm_error: Exception | None = None, reply_error: Exception | None = None
    ):
        self.provider = provider
        self.dm_error = dm_error
        self.reply_error = reply_error
        self.dms: list[tuple[str, str, dict[str, Any]]] = []
        # Instagram refusing button templates on a private reply, as it might.
        self.refuse_templates = False
        self.comments: dict[str, list[dict[str, Any]]] = {}

    def send_private_reply(self, ig_user_id: str, comment_id: str, message: dict[str, Any]) -> str:
        if self.dm_error:
            raise self.dm_error
        if self.refuse_templates and "attachment" in message:
            raise _graph_error(100)
        self.dms.append((ig_user_id, comment_id, message))
        return "m1"

    def reply_to_comment(self, comment_id: str, message: str) -> str:
        if self.reply_error:
            raise self.reply_error
        return "r1"

    def get_recent_comments(self, media_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.comments.get(media_id, [])


class FakeManager:
    def __init__(self, service: FakeService | None) -> None:
        self.service = service

    async def get_service_and_user_id(self, channel_id: str, prefer: str | None = None):
        return (self.service, "ig1") if self.service else None


BUTTON = {"title": "Open it", "url": "https://tryalgoviz.com"}


def _pending(**kw: Any) -> dict[str, Any]:
    return {
        "comment_id": "c1",
        "channel_id": "ch",
        "dm_text": "hi",
        "reply_text": "Follow for more",
        "attempts": 0,
        **kw,
    }


@pytest.mark.asyncio
class TestSendOne:
    async def test_dm_then_public_reply(self):
        db = DB(comment_dm_events=[_pending(status="sending")])
        service = FakeService()
        assert await send_one(db, FakeManager(service), _pending(), NOW) == "sent"
        doc = db.comment_dm_events.docs[0]
        assert (doc["status"], doc["dm_message_id"], doc["reply_comment_id"]) == ("sent", "m1", "r1")
        assert service.dms == [("ig1", "c1", {"text": "hi"})]

    async def test_buttons_go_as_a_button_template(self):
        db = DB(comment_dm_events=[_pending(status="sending")])
        service = FakeService()
        await send_one(db, FakeManager(service), _pending(buttons=[BUTTON]), NOW)
        [(_, _, message)] = service.dms
        payload = message["attachment"]["payload"]
        assert payload["template_type"] == "button" and payload["buttons"][0]["url"] == BUTTON["url"]
        assert db.comment_dm_events.docs[0]["dm_format"] == "buttons"

    async def test_refused_buttons_fall_back_to_links_in_the_text(self):
        db = DB(comment_dm_events=[_pending(status="sending")])
        service = FakeService()
        service.refuse_templates = True
        assert await send_one(db, FakeManager(service), _pending(buttons=[BUTTON]), NOW) == "sent"
        [(_, _, message)] = service.dms
        assert message == {"text": f"hi\n\nOpen it: {BUTTON['url']}"}
        doc = db.comment_dm_events.docs[0]
        assert doc["dm_format"] == "text_fallback" and "nope" in doc["button_error"]

    async def test_a_failed_public_reply_does_not_undo_the_dm(self):
        db = DB(comment_dm_events=[_pending(status="sending")])
        await send_one(db, FakeManager(FakeService(reply_error=_graph_error(100))), _pending(), NOW)
        doc = db.comment_dm_events.docs[0]
        assert doc["status"] == "sent" and "nope" in doc["reply_error"]

    async def test_rate_limits_are_retried_later(self):
        db = DB(comment_dm_events=[_pending(status="sending")])
        await send_one(db, FakeManager(FakeService(dm_error=_graph_error(4))), _pending(), NOW)
        doc = db.comment_dm_events.docs[0]
        assert doc["status"] == "pending" and doc["attempts"] == 1 and doc["send_after"] > NOW

    async def test_other_errors_fail_for_good(self):
        db = DB(comment_dm_events=[_pending(status="sending")])
        await send_one(db, FakeManager(FakeService(dm_error=_graph_error(100))), _pending(), NOW)
        assert db.comment_dm_events.docs[0]["status"] == "failed"

    async def test_the_hourly_cap_postpones_instead_of_sending(self):
        sent = [
            {"comment_id": f"s{i}", "channel_id": "ch", "status": "sent", "sent_at": NOW - timedelta(minutes=5)}
            for i in range(HOURLY_CAP)
        ]
        db = DB(comment_dm_events=[*sent, _pending(status="sending")])
        service = FakeService()
        assert await send_one(db, FakeManager(service), _pending(), NOW) == "pending"
        assert service.dms == []


@pytest.mark.asyncio
class TestPoll:
    async def test_queues_comments_read_with_a_facebook_token(self):
        db = _db_with_rule(_rule(), default=False)
        reader = FakeService(provider="facebook")
        comment = {"id": "c5", "text": "nice", "username": "ravi", "from": {"id": "u5"}}
        reader.comments["111111"] = [{**comment, "timestamp": "2026-10-25T06:29:00+0000"}]
        assert await poll_channel(db, FakeManager(reader), db.channels.docs[0], NOW) == 1

    async def test_an_instagram_login_reader_is_not_polled(self):
        db = _db_with_rule(_rule(), default=False)
        assert await poll_channel(db, FakeManager(FakeService(provider="instagram")), db.channels.docs[0], NOW) == 0


# --- webhook route ---------------------------------------------------------------------------


@pytest.fixture
def webhook_client():
    from app import main
    from app.database import get_db

    db = _db_with_rule(_rule())
    db.config.docs.append({"key": "instagram_webhook", "verify_token": "vt", "app_secret": "s3cret"})
    db.channels.docs[0][store.WEBHOOK_ID_FIELD] = "1784"
    # The route stamps comments with the real clock, so the rule has to be on since before it.
    db.channels.docs[0][store.DEFAULT_FIELD]["enabled_at"] = datetime.now(IST) - timedelta(days=1)
    main.app.dependency_overrides[get_db] = lambda: db
    yield TestClient(main.app), db
    main.app.dependency_overrides.pop(get_db, None)


def _signed(body: bytes, secret: str = "s3cret") -> dict[str, str]:
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}


def test_verification_echoes_the_challenge_only_for_our_token(webhook_client):
    client, _ = webhook_client

    def handshake(token: str):
        params = {"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "42"}
        return client.get("/api/v1/webhooks/instagram", params=params)

    ok = handshake("vt")
    assert ok.status_code == 200 and ok.text == "42"
    assert handshake("x").status_code == 403


def test_a_signed_comment_is_queued_and_an_unsigned_one_refused(webhook_client):
    client, db = webhook_client
    body = json.dumps(_comment_payload(at=datetime.now(IST))).encode()
    assert client.post("/api/v1/webhooks/instagram", content=body, headers=_signed(body, "wrong")).status_code == 403
    response = client.post("/api/v1/webhooks/instagram", content=body, headers=_signed(body))
    assert response.status_code == 200 and response.json() == {"comments": 1, "queued": 1}
    assert db.comment_dm_events.docs[0]["source"] == "webhook"


# --- dashboard routes ------------------------------------------------------------------------


@pytest.fixture
def api_client():
    from app import main
    from app.database import get_db
    from app.dependencies import verify_api_key

    db = DB(channels=[dict(CHANNEL)], videos=[{"channel_id": "ch", "instagram_media_id": "111111", "title": "Star"}])
    main.app.dependency_overrides[get_db] = lambda: db
    main.app.dependency_overrides[verify_api_key] = lambda: None
    yield TestClient(main.app), db
    main.app.dependency_overrides.pop(get_db, None)
    main.app.dependency_overrides.pop(verify_api_key, None)


def test_rules_round_trip_through_the_routes(api_client):
    client, _ = api_client
    base = "/api/v1/channels/ch/comment-dm"
    assert client.put(f"{base}/default", json={"dm_text": "hi", "triggers": ["Link"]}).status_code == 200
    assert client.put(f"{base}/reels/111111", json={"dm_text": "star", "enabled": False}).status_code == 200
    overview = client.get(base).json()
    assert overview["default"]["rule"]["triggers"] == ["link"] and overview["default"]["enabled_at"]
    assert overview["reels"] == [
        {"media_id": "111111", "title": "Star", "stored": overview["reels"][0]["stored"]},
    ]
    assert overview["reels"][0]["stored"]["enabled_at"] is None
    assert client.delete(f"{base}/reels/111111").json() == {"deleted": True}
    assert client.get(base).json()["reels"] == []


def test_a_non_numeric_media_id_is_refused(api_client):
    client, _ = api_client
    assert client.put("/api/v1/channels/ch/comment-dm/reels/abc", json={"dm_text": "hi"}).status_code == 422


def test_webhook_config_never_returns_the_secret(api_client):
    client, db = api_client
    first = client.get("/api/v1/comment-dm/webhook-config").json()
    assert first["verify_token"] and not first["has_app_secret"]
    saved = client.put("/api/v1/comment-dm/webhook-config", json={"app_secret": "a" * 32}).json()
    assert saved["has_app_secret"] and saved["verify_token"] == first["verify_token"]
    assert "app_secret" not in saved


def test_events_carry_their_reel_title(api_client):
    client, db = api_client
    db.comment_dm_events.docs.append(
        {"comment_id": "c1", "channel_id": "ch", "media_id": "111111", "status": "sent", "created_at": NOW}
    )
    [event] = client.get("/api/v1/channels/ch/comment-dm/events").json()
    assert event["title"] == "Star" and event["status"] == "sent"
