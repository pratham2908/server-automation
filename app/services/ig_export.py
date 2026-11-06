"""Reading Instagram's "Download your information" zip, in either of its formats (JSON or HTML).

Pure apart from reading the archive it is handed. Two things are read:

* **Followers** — each handle and when they followed (see ``follower_export``).
* **Comment-DM delivery** — in each DM thread, Instagram notes "You sent a private reply to a
  comment" for every private reply, and adds "This account can't receive your message…" (or "Not
  everyone can message this profile.") when the person doesn't accept message requests. The API
  reports both kinds of send identically, so the export is the only place that says which ones
  never arrived.

Times: the JSON export gives epoch timestamps. The HTML export prints wall-clock times shifted by
one fixed offset (the downloader's, e.g. UTC-8), and says so only indirectly: every page's header
has ``<time datetime="…Z">`` (true UTC) next to the same moment printed in that shifted clock.
The offset is read from there rather than guessed.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import IO, Any

from app.services.follower_export import ExportError, parse_followers_json

# A DM thread page is small; anything far bigger isn't one.
MAX_MEMBER_BYTES = 50 * 1024 * 1024

PRIVATE_REPLY = "You sent a private reply to a comment"
BLOCKED_MARKERS = ("can't receive your message", "can’t receive your message", "Not everyone can message this profile")

_HEADER_TIME = re.compile(r'<time datetime="(?P<iso>[^"]+)">(?P<text>[^<]+)</time>')
_HTML_STAMP = "%b %d, %Y %I:%M %p"
_TAG = re.compile(r"<[^>]+>")
_BLOCK = '<div class="pam _3-95 _2ph- _a6-g'
_FOLLOWER_ROW = re.compile(
    r'<a[^>]*href="https://www\.instagram\.com/(?:_u/)?(?P<handle>[^"/?]+)/?"[^>]*>[^<]*</a></div><div>(?P<at>[^<]+)</div>'
)
_DM_TIME = re.compile(r'<div class="_3-94 _a6-o">(?P<at>[^<]+)</div>')
_DM_BODY = re.compile(r'<div class="_3-95 _a6-p">(?P<body>.*?)</div><div class="_3-94 _a6-o">', re.S)
_DM_SENDER = re.compile(r"<h2[^>]*>(?P<name>.*?)</h2>", re.S)
_TITLE = re.compile(r"<h1[^>]*>(?P<title>.*?)</h1>", re.S)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(_TAG.sub(" ", fragment))).strip()


def _fix_mojibake(value: str) -> str:
    """The JSON export writes UTF-8 bytes as Latin-1 code points ("Ã©" for "é"); undo that."""
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def html_offset(page: str) -> tuple[timedelta, datetime] | None:
    """The shift of the page's printed times from UTC, and when the export was generated (UTC).

    The first header ``<time>`` is "Generated … on <time datetime=TRUE_UTC>PRINTED</time>".
    """
    match = _HEADER_TIME.search(page)
    if not match:
        return None
    try:
        true = datetime.fromisoformat(match["iso"].replace("Z", "+00:00"))
        printed = datetime.strptime(match["text"].replace(" UTC", "").split(" ", 1)[1], "%d %B %Y at %H:%M")
    except ValueError:
        return None
    true = true.astimezone(UTC).replace(tzinfo=None)
    # Round to the quarter hour: both are minute-precise, and real offsets are whole quarters.
    shift = round((printed - true).total_seconds() / 900) * 900
    return timedelta(seconds=shift), true.replace(tzinfo=UTC)


def parse_html_time(printed: str, offset: timedelta) -> datetime | None:
    """A printed HTML time ("Nov 06, 2026 4:26 am") as a UTC moment."""
    try:
        local = datetime.strptime(printed.strip().replace("am", "AM").replace("pm", "PM"), _HTML_STAMP)
    except ValueError:
        return None
    return (local - offset).replace(tzinfo=UTC)


def parse_followers_html(page: str, offset: timedelta) -> dict[str, datetime | None]:
    """Handle (lowercased) → when they followed, from one followers_N.html."""
    return {
        m["handle"].lower(): parse_html_time(html_lib.unescape(m["at"]), offset) for m in _FOLLOWER_ROW.finditer(page)
    }


@dataclass
class DmThread:
    """One DM thread's private replies: when each went out (UTC, minute precision) and whether it was refused."""

    thread: str
    title: str
    # The folder name's leading part: the person's handle, squashed ("" when the folder is only a number).
    handle_hint: str
    replies: list[tuple[datetime, bool]] = field(default_factory=list)


def _handle_hint(folder: str) -> str:
    head, _, tail = folder.rpartition("_")
    return re.sub(r"[^a-z0-9]", "", head.lower()) if head and tail.isdigit() else ""


def _pair(sent: list[datetime], refused: list[datetime]) -> list[tuple[datetime, bool]]:
    """Each private reply, refused when a refusal notice came within a minute of it."""
    left = sorted(refused)
    out: list[tuple[datetime, bool]] = []
    for at in sorted(sent):
        hit = next((r for r in left if abs((r - at).total_seconds()) <= 60), None)
        if hit is not None:
            left.remove(hit)
        out.append((at, hit is not None))
    return out


def thread_from_html(folder: str, page: str, offset: timedelta) -> DmThread | None:
    """The private replies in one thread's message_N.html, or None when it has none."""
    title_match = _TITLE.search(page)
    sent: list[datetime] = []
    refused: list[datetime] = []
    for block in page.split(_BLOCK)[1:]:
        body, when = _DM_BODY.search(block), _DM_TIME.search(block)
        if not body or not when:
            continue
        text, at = _text(body["body"]), parse_html_time(html_lib.unescape(when["at"]), offset)
        if at is None:
            continue
        if PRIVATE_REPLY in text:
            sent.append(at)
        elif any(marker in text for marker in BLOCKED_MARKERS):
            refused.append(at)
    if not sent:
        return None
    title = _text(title_match["title"]) if title_match else ""
    return DmThread(folder, title, _handle_hint(folder), _pair(sent, refused))


def thread_from_json(folder: str, data: Any) -> DmThread | None:
    """The private replies in one thread's message_N.json, or None when it has none."""
    sent: list[datetime] = []
    refused: list[datetime] = []
    for message in (data or {}).get("messages") or []:
        text = _fix_mojibake(str(message.get("content") or ""))
        stamp = message.get("timestamp_ms")
        if not isinstance(stamp, int | float):
            continue
        at = datetime.fromtimestamp(stamp / 1000, tz=UTC).replace(second=0, microsecond=0)
        if PRIVATE_REPLY in text:
            sent.append(at)
        elif any(marker in text for marker in BLOCKED_MARKERS):
            refused.append(at)
    if not sent:
        return None
    return DmThread(folder, _fix_mojibake(str(data.get("title") or "")), _handle_hint(folder), _pair(sent, refused))


_THREAD_FILE = re.compile(r"(^|/)messages/(inbox|message_requests)/(?P<folder>[^/]+)/message_\d+\.(?P<ext>html|json)$")
_FOLLOWERS_FILE = re.compile(r"(^|/)followers(_\d+)?\.(?P<ext>json|html)$", re.IGNORECASE)


class ExportArchive:
    """An export zip, read member by member so a large one never sits in memory whole."""

    def __init__(self, source: IO[bytes]) -> None:
        try:
            self.zip = zipfile.ZipFile(source)
        except zipfile.BadZipFile as exc:
            raise ExportError("That zip couldn't be opened — download the export again.") from exc
        self.names = self.zip.namelist()
        self._offset: tuple[timedelta, datetime] | None = None

    def _read(self, name: str) -> str:
        if self.zip.getinfo(name).file_size > MAX_MEMBER_BYTES:
            raise ExportError(f"{name} is too large for an export file.")
        return self.zip.read(name).decode("utf-8", errors="replace")

    def offset(self) -> tuple[timedelta, datetime] | None:
        """The HTML clock shift and generation time, from the first page that has a header."""
        if self._offset is None:
            for name in self.names:
                if name.lower().endswith(".html"):
                    found = html_offset(self._read(name))
                    if found:
                        self._offset = found
                        break
        return self._offset

    def generated_at(self) -> datetime | None:
        found = self.offset()
        return found[1] if found else None

    def followers(self) -> dict[str, datetime | None] | None:
        """Every follower in the export, or None when it has no followers file."""
        names = [n for n in self.names if _FOLLOWERS_FILE.search(n) and "followers_and_following" in n]
        if not names:
            return None
        out: dict[str, datetime | None] = {}
        for name in names:
            if name.lower().endswith(".json"):
                try:
                    out.update(parse_followers_json(json.loads(self._read(name))))
                except ValueError as exc:
                    raise ExportError(f"{name} isn't valid JSON.") from exc
            else:
                shift = self.offset()
                if shift is None:
                    raise ExportError("Couldn't read the times in this HTML export; request it as JSON instead.")
                out.update(parse_followers_html(self._read(name), shift[0]))
        return out

    def dm_threads(self) -> Iterator[DmThread]:
        """Every DM thread that holds a private reply to a comment."""
        for name in self.names:
            match = _THREAD_FILE.search(name)
            if not match:
                continue
            if match["ext"] == "json":
                try:
                    thread = thread_from_json(match["folder"], json.loads(self._read(name)))
                except ValueError:
                    continue
            else:
                shift = self.offset()
                if shift is None:
                    continue
                thread = thread_from_html(match["folder"], self._read(name), shift[0])
            if thread:
                yield thread
