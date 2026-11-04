"""What one DM can carry, and how it becomes the ``message`` objects Instagram's Send API takes.

Pure: models, validation, and builders, so every shape is testable without Meta or Mongo.

A ``DmMessage`` is what the owner composes — text with up to 3 buttons and up to 13 quick-reply chips,
or a carousel of up to 10 cards, optionally with a picture, video, voice note or heart sticker sent
first. Instagram sends those as separate messages, so ``build_messages`` returns a list.

A button or chip can SEND A REPLY when tapped. Instagram hands us only the tap's ``payload`` string
(≤1000 chars), so the reply is stored by a content hash (``reply_id``) and the payload names it:
``dmr:<id>``. The same content always gets the same id, so saving a rule twice registers nothing new
and an old DM's buttons keep working after the rule is edited.

Instagram's limits (Messenger Platform for Instagram, 2026): a button template takes ≤640 characters
of text and ≤3 buttons with ≤20-character labels; a generic template ≤10 elements, each with a title
and subtitle ≤80 and ≤3 buttons; ≤13 quick replies with ≤20-character titles; a text message ≤1000.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DM_TEXT_LIMIT = 1000
BUTTON_TEXT_LIMIT = 640
MAX_BUTTONS = 3
BUTTON_TITLE_LIMIT = 20
MAX_CARDS = 10
CARD_TEXT_LIMIT = 80
MAX_QUICK_REPLIES = 13
QUICK_REPLY_TITLE_LIMIT = 20
# A reply can carry buttons that send replies of their own; three taps deep is a flow, deeper is a maze.
MAX_REPLY_DEPTH = 3

REPLY_PAYLOAD_PREFIX = "dmr:"
# A chip with no reply still needs a payload; this one marks it as ours and carries nothing to act on.
PLAIN_CHIP_PREFIX = "qr:"

URL_PATTERN = r"^https?://\S+$"

ButtonType = Literal["url", "profile", "reply"]
MediaType = Literal["image", "video", "audio", "sticker"]


def _not_blank(value: str, what: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{what} must not be blank")
    return stripped


class DmMedia(BaseModel):
    """A picture, video or voice note sent before the text, or Instagram's heart sticker.

    Either a public ``url`` or an ``r2_key`` for a file uploaded here; the key is turned into a fresh
    signed link at send time, since a stored link would have expired by the time a tap uses it.
    """

    type: MediaType
    url: str | None = Field(default=None, max_length=2000, pattern=URL_PATTERN)
    r2_key: str | None = Field(default=None, max_length=300, pattern=r"^dm-media/[A-Za-z0-9._/-]+$")

    @model_validator(mode="after")
    def _has_a_source(self) -> DmMedia:
        if self.type == "sticker":
            if self.url or self.r2_key:
                raise ValueError("the heart sticker takes no file")
        elif bool(self.url) == bool(self.r2_key):
            raise ValueError(f"a {self.type} needs exactly one of a link or an uploaded file")
        return self


class DmButton(BaseModel):
    """One button under a message or a card.

    - ``url`` opens a link;
    - ``profile`` opens our Instagram profile (Instagram has no follow button; this is the closest);
    - ``reply`` sends ``reply`` back into the chat when tapped, optionally only to followers.

    A rule saved before button types existed has only ``title`` and ``url``, which reads as ``url``.
    """

    type: ButtonType = "url"
    title: str = Field(min_length=1, max_length=BUTTON_TITLE_LIMIT)
    url: str | None = Field(default=None, max_length=2000, pattern=URL_PATTERN)
    reply: DmMessage | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        return _not_blank(value, "button title")

    @model_validator(mode="after")
    def _fits_its_type(self) -> DmButton:
        if self.type == "url" and not self.url:
            raise ValueError(f"button '{self.title}' needs a link")
        if self.type == "reply" and self.reply is None:
            raise ValueError(f"button '{self.title}' needs a reply to send")
        if self.type != "url":
            self.url = None
        if self.type != "reply":
            self.reply = None
        return self


class DmCard(BaseModel):
    """One card of a carousel: an image, a title, a line under it, and up to 3 buttons."""

    title: str = Field(min_length=1, max_length=CARD_TEXT_LIMIT)
    subtitle: str = Field(default="", max_length=CARD_TEXT_LIMIT)
    image: DmMedia | None = None
    # Where tapping the card itself (not a button) goes.
    link: str | None = Field(default=None, max_length=2000, pattern=URL_PATTERN)
    buttons: list[DmButton] = Field(default_factory=list, max_length=MAX_BUTTONS)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        return _not_blank(value, "card title")

    @field_validator("subtitle")
    @classmethod
    def _strip_subtitle(cls, value: str) -> str:
        return value.strip()

    @field_validator("image")
    @classmethod
    def _image_only(cls, value: DmMedia | None) -> DmMedia | None:
        if value is not None and value.type != "image":
            raise ValueError("a card's picture must be an image")
        return value


class DmQuickReply(BaseModel):
    """A chip under the message. Tapping it sends its title as their message, and ``reply`` back if set."""

    title: str = Field(min_length=1, max_length=QUICK_REPLY_TITLE_LIMIT)
    reply: DmMessage | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        return _not_blank(value, "chip title")


class DmMessage(BaseModel):
    """Everything one DM sends: optional media first, then text with buttons and chips, or a carousel."""

    text: str = Field(default="", max_length=DM_TEXT_LIMIT)
    buttons: list[DmButton] = Field(default_factory=list, max_length=MAX_BUTTONS)
    cards: list[DmCard] = Field(default_factory=list, max_length=MAX_CARDS)
    quick_replies: list[DmQuickReply] = Field(default_factory=list, max_length=MAX_QUICK_REPLIES)
    media: DmMedia | None = None
    # Only for a reply a tap sends: check they follow us first, and if not, send this instead with a
    # button to check again. Empty means no check.
    followers_only_text: str = Field(default="", max_length=BUTTON_TEXT_LIMIT)

    @field_validator("text", "followers_only_text")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _is_one_shape(self) -> DmMessage:
        if self.cards and (self.text or self.buttons):
            raise ValueError("a carousel carries its text and buttons on its cards, not beside them")
        if self.buttons and len(self.text) > BUTTON_TEXT_LIMIT:
            raise ValueError(f"with buttons, the message must be at most {BUTTON_TEXT_LIMIT} characters")
        if self.buttons and not self.text:
            raise ValueError("buttons need a message above them")
        if self.quick_replies and not self.text:
            # Chips ride a text message; under a carousel or media alone Instagram drops them.
            raise ValueError("quick replies need a message above them")
        if not (self.text or self.cards or self.media):
            raise ValueError("the message is empty")
        if depth(self) > MAX_REPLY_DEPTH:
            raise ValueError(f"replies can be nested at most {MAX_REPLY_DEPTH} taps deep")
        return self

    @property
    def sends_one_message(self) -> bool:
        """Whether this goes out as a single Instagram message — all a comment's private reply may be."""
        return self.media is None


def _children(message: DmMessage) -> list[DmMessage]:
    buttons = [*message.buttons, *(b for c in message.cards for b in c.buttons)]
    replies = [b.reply for b in buttons if b.reply is not None]
    replies += [q.reply for q in message.quick_replies if q.reply is not None]
    return replies


def depth(message: DmMessage) -> int:
    """How many taps deep its replies go: 0 for a message with no reply buttons."""
    kids = _children(message)
    return 1 + max(depth(k) for k in kids) if kids else 0


def replies_in(message: DmMessage) -> list[DmMessage]:
    """Every reply a tap on *message* (or on a reply of it) can send, outermost first."""
    found: list[DmMessage] = []
    for child in _children(message):
        found.append(child)
        found.extend(replies_in(child))
    return found


def reply_id(channel_id: str, reply: DmMessage) -> str:
    """The stable id a tap's payload names this reply by. Same channel and content, same id."""
    canonical = json.dumps(reply.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{channel_id}\n{canonical}".encode()).hexdigest()[:20]


def reply_payload(channel_id: str, reply: DmMessage) -> str:
    return f"{REPLY_PAYLOAD_PREFIX}{reply_id(channel_id, reply)}"


def payload_reply_id(payload: str) -> str | None:
    """The reply a tap asks for, or None when the payload isn't one of ours that sends something."""
    if not payload.startswith(REPLY_PAYLOAD_PREFIX):
        return None
    rid = payload.removeprefix(REPLY_PAYLOAD_PREFIX)
    return rid if rid.isalnum() and 0 < len(rid) <= 64 else None


def profile_url(username: str) -> str:
    return f"https://www.instagram.com/{username.strip().lstrip('@')}/"


class SendContext(BaseModel):
    """What a build needs from outside the message: our handle, and signed links for uploaded files."""

    channel_id: str
    username: str = ""
    media_urls: dict[str, str] = Field(default_factory=dict)


def media_keys(message: DmMessage) -> list[str]:
    """Uploaded files *message* itself shows (not its replies'), which the sender must sign links for."""
    items = [message.media, *(c.image for c in message.cards)]
    return [m.r2_key for m in items if m is not None and m.r2_key]


def _media_url(media: DmMedia, ctx: SendContext) -> str:
    if media.url:
        return media.url
    signed = ctx.media_urls.get(media.r2_key or "")
    if not signed:
        raise ValueError(f"no link for uploaded file {media.r2_key}")
    return signed


def _button(button: DmButton, ctx: SendContext) -> dict[str, Any]:
    if button.type == "reply" and button.reply is not None:
        return {"type": "postback", "title": button.title, "payload": reply_payload(ctx.channel_id, button.reply)}
    if button.type == "profile":
        if not ctx.username:
            raise ValueError("the profile button needs the channel's Instagram username")
        return {"type": "web_url", "url": profile_url(ctx.username), "title": button.title}
    return {"type": "web_url", "url": button.url, "title": button.title}


def _card(card: DmCard, ctx: SendContext) -> dict[str, Any]:
    element: dict[str, Any] = {"title": card.title}
    if card.subtitle:
        element["subtitle"] = card.subtitle
    if card.image is not None:
        element["image_url"] = _media_url(card.image, ctx)
    if card.link:
        element["default_action"] = {"type": "web_url", "url": card.link}
    if card.buttons:
        element["buttons"] = [_button(b, ctx) for b in card.buttons]
    return element


def _chip(chip: DmQuickReply, ctx: SendContext) -> dict[str, Any]:
    payload = reply_payload(ctx.channel_id, chip.reply) if chip.reply else f"{PLAIN_CHIP_PREFIX}{chip.title}"
    return {"content_type": "text", "title": chip.title, "payload": payload[:1000]}


def media_message(media: DmMedia, ctx: SendContext) -> dict[str, Any]:
    if media.type == "sticker":
        return {"attachment": {"type": "like_heart"}}
    return {"attachment": {"type": media.type, "payload": {"url": _media_url(media, ctx)}}}


def main_message(message: DmMessage, ctx: SendContext) -> dict[str, Any] | None:
    """The text, button template or carousel — the part after any media. None for media alone."""
    built: dict[str, Any]
    if message.cards:
        elements = [_card(c, ctx) for c in message.cards]
        built = {"attachment": {"type": "template", "payload": {"template_type": "generic", "elements": elements}}}
    elif message.buttons:
        payload = {
            "template_type": "button",
            "text": message.text,
            "buttons": [_button(b, ctx) for b in message.buttons],
        }
        built = {"attachment": {"type": "template", "payload": payload}}
    elif message.text:
        built = {"text": message.text}
    else:
        return None
    if message.quick_replies:
        built["quick_replies"] = [_chip(q, ctx) for q in message.quick_replies]
    return built


def build_messages(message: DmMessage, ctx: SendContext) -> list[dict[str, Any]]:
    """The Send API ``message`` objects for *message*, in the order they go out."""
    out: list[dict[str, Any]] = []
    if message.media is not None:
        out.append(media_message(message.media, ctx))
    main = main_message(message, ctx)
    if main is not None:
        out.append(main)
    return out


def _button_line(button: DmButton, ctx: SendContext) -> str | None:
    if button.type == "url":
        return f"{button.title}: {button.url}"
    if button.type == "profile" and ctx.username:
        return f"{button.title}: {profile_url(ctx.username)}"
    # A reply button has nothing to write out: without the tap there is no reply.
    return None


def fallback_text(message: DmMessage, ctx: SendContext) -> str:
    """The message as plain text, for when Instagram refuses a template on a comment's private reply.

    Links are written out; reply buttons and chips can't survive as text, so they're dropped.
    """
    parts: list[str] = []
    if message.text:
        parts.append(message.text)
    for card in message.cards:
        lines = [card.title, card.subtitle, card.link or ""]
        lines += [line for b in card.buttons if (line := _button_line(b, ctx))]
        parts.append("\n".join(x for x in lines if x))
    links = [line for b in message.buttons if (line := _button_line(b, ctx))]
    if links:
        parts.append("\n".join(links))
    return "\n\n".join(parts)[:DM_TEXT_LIMIT]


def has_template(message: DmMessage) -> bool:
    """Whether the main message is more than plain text — the part Instagram might refuse."""
    return bool(message.cards or message.buttons or message.quick_replies)


def summary_text(message: DmMessage) -> str:
    """One line for the inbox's stored copy of what we sent."""
    if message.text:
        return message.text
    if message.cards:
        titles = ", ".join(c.title for c in message.cards)
        return f"[{len(message.cards)} card{'s' if len(message.cards) != 1 else ''}] {titles}"
    if message.media is not None:
        return "[heart]" if message.media.type == "sticker" else f"[{message.media.type}]"
    return ""


def following_gate(message: DmMessage, channel_id: str, again_title: str = "I followed ✅") -> DmMessage:
    """What a non-follower gets instead of *message*: the gate text and a button that checks again."""
    return DmMessage(
        text=message.followers_only_text,
        buttons=[
            DmButton(type="profile", title="Open profile"),
            DmButton(type="reply", title=again_title, reply=message),
        ],
    )


DmButton.model_rebuild()
DmQuickReply.model_rebuild()
DmCard.model_rebuild()
DmMessage.model_rebuild()


# --- Account-level: ice breakers and the persistent menu -------------------------------------------------

MAX_ICE_BREAKERS = 4
ICE_BREAKER_LIMIT = 80
MAX_MENU_ITEMS = 5
MENU_TITLE_LIMIT = 30


class IceBreaker(BaseModel):
    """A question shown to someone opening a chat with us for the first time; tapping it sends ``reply``."""

    question: str = Field(min_length=1, max_length=ICE_BREAKER_LIMIT)
    reply: DmMessage

    @field_validator("question")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _not_blank(value, "question")


class MenuItem(BaseModel):
    """One line of the chat's menu: opens a link, or sends ``reply``."""

    title: str = Field(min_length=1, max_length=MENU_TITLE_LIMIT)
    type: Literal["url", "reply"] = "url"
    url: str | None = Field(default=None, max_length=2000, pattern=URL_PATTERN)
    reply: DmMessage | None = None

    @field_validator("title")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _not_blank(value, "menu title")

    @model_validator(mode="after")
    def _fits_its_type(self) -> MenuItem:
        if self.type == "url" and not self.url:
            raise ValueError(f"menu item '{self.title}' needs a link")
        if self.type == "reply" and self.reply is None:
            raise ValueError(f"menu item '{self.title}' needs a reply to send")
        return self


class InboxSettings(BaseModel):
    """The account's chat furniture. Saved here first; Instagram only sees it once published."""

    ice_breakers: list[IceBreaker] = Field(default_factory=list, max_length=MAX_ICE_BREAKERS)
    menu: list[MenuItem] = Field(default_factory=list, max_length=MAX_MENU_ITEMS)


def settings_replies(settings: InboxSettings) -> list[DmMessage]:
    """Every reply the settings can send, nested ones included, so each can be registered."""
    tops = [b.reply for b in settings.ice_breakers] + [m.reply for m in settings.menu if m.reply is not None]
    return [r for top in tops for r in (top, *replies_in(top))]


def ice_breakers_body(settings: InboxSettings, channel_id: str) -> dict[str, Any]:
    """``POST /me/messenger_profile`` body for the ice breakers."""
    actions = [{"question": b.question, "payload": reply_payload(channel_id, b.reply)} for b in settings.ice_breakers]
    return {"platform": "instagram", "ice_breakers": [{"call_to_actions": actions, "locale": "default"}]}


def menu_body(settings: InboxSettings, channel_id: str) -> dict[str, Any]:
    """``POST /me/messenger_profile`` body for the persistent menu."""
    actions: list[dict[str, Any]] = []
    for item in settings.menu:
        if item.type == "reply" and item.reply is not None:
            actions.append({"type": "postback", "title": item.title, "payload": reply_payload(channel_id, item.reply)})
        else:
            actions.append({"type": "web_url", "title": item.title, "url": item.url})
    return {
        "platform": "instagram",
        "persistent_menu": [{"locale": "default", "composer_input_disabled": False, "call_to_actions": actions}],
    }
