"""Turning Meta's conversation and message objects into what the dashboard shows.

Pure: no I/O. Meta nests participants and messages under ``data`` and names the sender by
handle, so which side is us is worked out here from the account's own username.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from dateutil.parser import isoparse
from pydantic import BaseModel

from app.timezone import to_ist_iso

DmKind = Literal["text", "attachment", "share", "story", "unsupported"]


class DmMessageOut(BaseModel):
    id: str
    created_at: str | None = None
    from_me: bool
    sender: str | None = None
    text: str | None = None
    kind: DmKind = "text"
    # Where a share, story or attachment points, when Meta gave a link.
    link: str | None = None


class DmConversationOut(BaseModel):
    id: str
    with_username: str | None = None
    with_id: str | None = None
    updated_at: str | None = None
    last_message: DmMessageOut | None = None


def _data(value: Any) -> list[Mapping[str, Any]]:
    """The list under Meta's ``{"data": [...]}`` wrapper; empty when absent or malformed."""
    items = value.get("data") if isinstance(value, Mapping) else None
    return [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []


def _first_link(item: Mapping[str, Any]) -> str | None:
    """The first URL-looking value in a share, story or attachment object, however Meta nested it."""
    for key in ("link", "url", "file_url"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    for nested in item.values():
        if isinstance(nested, Mapping):
            found = _first_link(nested)
            if found:
                return found
    return None


def _time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return to_ist_iso(isoparse(value))
    except ValueError:
        return None


def _same_handle(a: Any, b: str | None) -> bool:
    return isinstance(a, str) and b is not None and a.casefold() == b.casefold()


def parse_message(raw: Mapping[str, Any], own_username: str | None) -> DmMessageOut:
    sender = raw.get("from") if isinstance(raw.get("from"), Mapping) else {}
    kind: DmKind = "text"
    link: str | None = None
    shares, attachments, story = _data(raw.get("shares")), _data(raw.get("attachments")), raw.get("story")
    if raw.get("is_unsupported"):
        kind = "unsupported"
    elif shares:
        kind, link = "share", _first_link(shares[0])
    elif isinstance(story, Mapping) and story:
        kind, link = "story", _first_link(story)
    elif attachments:
        kind, link = "attachment", _first_link(attachments[0])
    text = raw.get("message")
    return DmMessageOut(
        id=str(raw.get("id", "")),
        created_at=_time(raw.get("created_time")),
        from_me=_same_handle(sender.get("username"), own_username),
        sender=sender.get("username") if isinstance(sender.get("username"), str) else None,
        text=text if isinstance(text, str) and text else None,
        kind=kind,
        link=link,
    )


def parse_conversation(raw: Mapping[str, Any], own_username: str | None) -> DmConversationOut:
    others = [p for p in _data(raw.get("participants")) if not _same_handle(p.get("username"), own_username)]
    other = others[0] if others else {}
    latest = _data(raw.get("messages"))
    return DmConversationOut(
        id=str(raw.get("id", "")),
        with_username=other.get("username") if isinstance(other.get("username"), str) else None,
        with_id=str(other["id"]) if other.get("id") else None,
        updated_at=_time(raw.get("updated_time")),
        last_message=parse_message(latest[0], own_username) if latest else None,
    )


def parse_thread(raw_messages: Sequence[Mapping[str, Any]], own_username: str | None) -> list[DmMessageOut]:
    """Messages oldest first — Meta sends them newest first, but a thread reads downward."""
    return [parse_message(m, own_username) for m in reversed(raw_messages)]


def next_cursor(page: Mapping[str, Any]) -> str | None:
    """The cursor for the next page, only when Meta says there is one."""
    paging = page.get("paging")
    if not isinstance(paging, Mapping) or not paging.get("next"):
        return None
    cursors = paging.get("cursors")
    after = cursors.get("after") if isinstance(cursors, Mapping) else None
    return str(after) if after else None
