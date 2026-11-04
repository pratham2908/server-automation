"""What a DM can carry, and the Send API objects it becomes."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.services.comment_dm import DmRule
from app.services.dm_content import (
    MAX_REPLY_DEPTH,
    DmButton,
    DmMessage,
    InboxSettings,
    SendContext,
    build_messages,
    depth,
    fallback_text,
    following_gate,
    ice_breakers_body,
    media_keys,
    menu_body,
    payload_reply_id,
    replies_in,
    reply_id,
    reply_payload,
    settings_replies,
    summary_text,
)

CTX = SendContext(channel_id="ch", username="tryalgoviz", media_urls={"dm-media/ch/a.jpg": "https://signed/a.jpg"})
LINK = {"title": "Open it", "url": "https://tryalgoviz.com"}


def _msg(**kw: Any) -> DmMessage:
    return DmMessage.model_validate(kw)


def test_plain_text_is_a_text_message() -> None:
    assert build_messages(_msg(text="hi"), CTX) == [{"text": "hi"}]


def test_a_rule_saved_before_button_types_reads_as_link_buttons() -> None:
    button = DmButton.model_validate(LINK)
    assert (button.type, button.url) == ("url", LINK["url"])


def test_buttons_of_each_kind_become_the_button_template() -> None:
    reply = {"text": "Here you go"}
    message = _msg(
        text="Pick one",
        buttons=[
            LINK,
            {"type": "profile", "title": "Follow us"},
            {"type": "reply", "title": "Send it", "reply": reply},
        ],
    )
    [built] = build_messages(message, CTX)
    payload = built["attachment"]["payload"]
    assert payload["template_type"] == "button" and payload["text"] == "Pick one"
    link, profile, postback = payload["buttons"]
    assert link == {"type": "web_url", "url": LINK["url"], "title": "Open it"}
    assert profile == {"type": "web_url", "url": "https://www.instagram.com/tryalgoviz/", "title": "Follow us"}
    assert postback["type"] == "postback" and postback["payload"] == reply_payload("ch", _msg(**reply))


def test_a_profile_button_without_a_handle_cannot_be_built() -> None:
    message = _msg(text="hi", buttons=[{"type": "profile", "title": "Follow us"}])
    with pytest.raises(ValueError, match="username"):
        build_messages(message, SendContext(channel_id="ch"))


def test_cards_become_a_generic_template_with_signed_images() -> None:
    card = {
        "title": "Graphs",
        "subtitle": "BFS step by step",
        "image": {"type": "image", "r2_key": "dm-media/ch/a.jpg"},
        "link": "https://tryalgoviz.com/bfs",
        "buttons": [LINK],
    }
    [built] = build_messages(_msg(cards=[card, {"title": "Trees"}]), CTX)
    first, second = built["attachment"]["payload"]["elements"]
    assert built["attachment"]["payload"]["template_type"] == "generic"
    assert first["image_url"] == "https://signed/a.jpg" and first["default_action"]["url"].endswith("/bfs")
    assert first["buttons"][0]["type"] == "web_url" and second == {"title": "Trees"}


def test_media_goes_first_then_the_text_with_its_chips() -> None:
    message = _msg(
        media={"type": "video", "url": "https://cdn.example/v.mp4"},
        text="Watch this",
        quick_replies=[{"title": "More"}, {"title": "Link", "reply": {"text": "here"}}],
    )
    media, main = build_messages(message, CTX)
    assert media == {"attachment": {"type": "video", "payload": {"url": "https://cdn.example/v.mp4"}}}
    assert main["text"] == "Watch this"
    plain, linked = main["quick_replies"]
    assert plain == {"content_type": "text", "title": "More", "payload": "qr:More"}
    assert payload_reply_id(linked["payload"]) == reply_id("ch", _msg(text="here"))


def test_the_heart_sticker_is_its_own_message() -> None:
    assert build_messages(_msg(media={"type": "sticker"}), CTX) == [{"attachment": {"type": "like_heart"}}]


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"cards": [{"title": "a"}], "text": "and text"},
        {"buttons": [LINK]},
        {"quick_replies": [{"title": "x"}], "cards": [{"title": "a"}]},
        {"text": "x" * 641, "buttons": [LINK]},
        {"text": "hi", "buttons": [LINK] * 4},
        {"cards": [{"title": "a"}] * 11},
        {"text": "hi", "quick_replies": [{"title": "x"}] * 14},
        {"text": "hi", "buttons": [{"type": "reply", "title": "x"}]},
        {"text": "hi", "buttons": [{"type": "url", "title": "x"}]},
        {"media": {"type": "image"}},
        {"media": {"type": "image", "url": "https://a/b.jpg", "r2_key": "dm-media/x.jpg"}},
        {"media": {"type": "sticker", "url": "https://a/b.jpg"}},
        {"cards": [{"title": "a", "image": {"type": "video", "url": "https://a/b.mp4"}}]},
        {"media": {"type": "image", "r2_key": "../etc/passwd"}},
    ],
)
def test_shapes_instagram_would_refuse_are_refused_here(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DmMessage.model_validate(bad)


def _nested(levels: int) -> dict[str, Any]:
    message: dict[str, Any] = {"text": "end"}
    for _ in range(levels):
        message = {"text": "tap", "buttons": [{"type": "reply", "title": "next", "reply": message}]}
    return message


def test_replies_nest_only_so_deep() -> None:
    assert depth(_msg(**_nested(MAX_REPLY_DEPTH))) == MAX_REPLY_DEPTH
    with pytest.raises(ValidationError, match="taps deep"):
        DmMessage.model_validate(_nested(MAX_REPLY_DEPTH + 1))


def test_every_nested_reply_is_found_to_register() -> None:
    found = replies_in(_msg(**_nested(2)))
    assert [m.text for m in found] == ["tap", "end"]


def test_a_reply_id_is_its_content_and_its_channel() -> None:
    a, b = _msg(text="x"), _msg(text="x")
    assert reply_id("ch", a) == reply_id("ch", b) != reply_id("other", a) != reply_id("ch", _msg(text="y"))
    assert payload_reply_id(reply_payload("ch", a)) == reply_id("ch", a)
    assert payload_reply_id("qr:More") is None and payload_reply_id("dmr:../x") is None


def test_the_fallback_writes_out_every_link_and_drops_what_needs_a_tap() -> None:
    message = _msg(
        text="Here",
        buttons=[LINK, {"type": "profile", "title": "Us"}, {"type": "reply", "title": "Tap", "reply": {"text": "r"}}],
    )
    text = fallback_text(message, CTX)
    assert "Open it: https://tryalgoviz.com" in text and "Us: https://www.instagram.com/tryalgoviz/" in text
    assert "Tap" not in text
    cards = fallback_text(_msg(cards=[{"title": "Graphs", "subtitle": "BFS", "buttons": [LINK]}]), CTX)
    assert cards.splitlines()[:2] == ["Graphs", "BFS"] and LINK["url"] in cards


def test_a_follower_gate_offers_the_profile_and_a_check_again_that_names_the_same_reply() -> None:
    reply = _msg(text="The link", followers_only_text="Follow us first!")
    gate = following_gate(reply, "ch")
    [built] = build_messages(gate, CTX)
    profile, again = built["attachment"]["payload"]["buttons"]
    assert built["attachment"]["payload"]["text"] == "Follow us first!"
    assert profile["url"].endswith("/tryalgoviz/") and again["payload"] == reply_payload("ch", reply)


def test_the_inbox_copy_says_what_went() -> None:
    assert summary_text(_msg(cards=[{"title": "A"}, {"title": "B"}])) == "[2 cards] A, B"
    assert summary_text(_msg(media={"type": "sticker"})) == "[heart]"


def test_only_uploaded_files_need_signing() -> None:
    message = _msg(cards=[{"title": "a", "image": {"type": "image", "r2_key": "dm-media/ch/a.jpg"}}])
    assert media_keys(message) == ["dm-media/ch/a.jpg"]
    assert media_keys(_msg(media={"type": "image", "url": "https://a/b.jpg"})) == []


def test_a_comment_rule_can_carry_cards_and_chips_but_not_nothing() -> None:
    rule = DmRule(cards=[{"title": "Graphs"}])
    assert rule.message().cards[0].title == "Graphs"
    assert DmRule(dm_text="hi", quick_replies=[{"title": "More"}]).message().quick_replies[0].title == "More"
    with pytest.raises(ValidationError):
        DmRule(dm_text="  ")


def test_ice_breakers_and_menu_are_messenger_profile_bodies() -> None:
    settings = InboxSettings.model_validate(
        {
            "ice_breakers": [{"question": "What is this?", "reply": {"text": "Algorithm visuals"}}],
            "menu": [
                {"title": "Website", "type": "url", "url": "https://tryalgoviz.com"},
                {"title": "Get the link", "type": "reply", "reply": {"text": "here", "buttons": [LINK]}},
            ],
        }
    )
    breakers = ice_breakers_body(settings, "ch")
    [locale] = breakers["ice_breakers"]
    assert breakers["platform"] == "instagram" and locale["locale"] == "default"
    assert locale["call_to_actions"] == [
        {"question": "What is this?", "payload": reply_payload("ch", _msg(text="Algorithm visuals"))}
    ]
    site, postback = menu_body(settings, "ch")["persistent_menu"][0]["call_to_actions"]
    assert site == {"type": "web_url", "title": "Website", "url": "https://tryalgoviz.com"}
    assert postback["type"] == "postback" and postback["payload"].startswith("dmr:")
    assert [m.text for m in settings_replies(settings)] == ["Algorithm visuals", "here"]
