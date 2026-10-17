"""The story kit: everything the owner needs to schedule an Instagram story for a reel by hand.

Instagram's API cannot add a link sticker, and stories can only be scheduled in the Meta Business Suite
phone app, so the owner does it on the phone. This module is the pure part: the link, the time, the
token, and the page and email markup. The routes and the database live in ``app/routers/story_kit.py``.

A kit is one page behind an unguessable token. Each reel on it is a card with ONE button that, in one tap,
copies the story link and opens the iPhone share sheet with the video file so it can be saved to Photos
(Business Suite's gallery picker cannot see Files, and is not in the share sheet).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from html import escape
from typing import Any, NamedTuple
from urllib.parse import quote

from app.timezone import IST, assume_utc

SITE = "https://tryalgoviz.com"
# The story goes up this long after its reel: far enough that the reel's first minutes are not diluted,
# close enough that followers who saw the reel still see the story.
STORY_DELAY = timedelta(minutes=7)
# A forwarded email must not leak our files for long.
KIT_LIFETIME = timedelta(days=3)
STICKER_TEXT = "Play this one yourself"

_INK, _MUTED, _LINE, _PAGE, _CARD, _ACCENT = "#0f172a", "#64748b", "#e2e8f0", "#f1f5f9", "#ffffff", "#2563eb"
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


class KitItem(NamedTuple):
    video_id: str
    slug: str
    title: str
    reel_time: datetime  # when the reel goes live, tz-aware
    # In the email: a link to the cover image. On the page: the cover's storage key (the page serves it itself).
    thumbnail_url: str | None = None

    @property
    def story_time(self) -> datetime:
        return self.reel_time + STORY_DELAY


def new_token() -> str:
    """43 url-safe characters: the page has no login, so the token is the only lock."""
    return secrets.token_urlsafe(32)


def story_link(slug: str) -> str:
    """The sticker's link. `/r/` sends the slug to its visualizer and keeps the allow-listed medium."""
    return f"{SITE}/r/{quote(slug, safe='')}/?utm_medium=story&utm_campaign={quote(slug, safe='')}"


def reel_time_ist(scheduled_at: datetime) -> datetime:
    """Mongo hands back naive UTC; the owner reads IST."""
    return assume_utc(scheduled_at).astimezone(IST)


def clock(dt: datetime) -> str:
    """'9:37 AM' (no leading zero, no platform-specific strftime flags)."""
    local = dt.astimezone(IST)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def day_clock(dt: datetime) -> str:
    return f"{dt.astimezone(IST).strftime('%a %d %b')}, {clock(dt)} IST"


def is_expired(expires_at: datetime, now: datetime) -> bool:
    return assume_utc(now) >= assume_utc(expires_at)


# --- the email -------------------------------------------------------------------------------------


def kit_email(items: list[KitItem], page_url: str) -> tuple[str, str, str]:
    """Subject, text and html for one scheduling run. One row per reel, one button to the kit."""
    n = len(items)
    subject = f"Stories to schedule: {n} reel{'s' if n != 1 else ''}"
    lines = [f"{n} reel{'s' if n != 1 else ''} just scheduled. Schedule a story for each:", ""]
    for it in items:
        lines.append(f"- {it.title}: story at {day_clock(it.story_time)}")
    lines += ["", f"Open on your phone: {page_url}", "", "The page works for 3 days."]
    rows = "".join(
        f'<tr><td style="padding:12px 0;border-top:1px solid {_LINE};font-family:{_FONT};color:{_INK}">'
        + (
            f'<img src="{escape(it.thumbnail_url, quote=True)}" width="54" style="float:left;margin-right:12px;border-radius:6px" alt="">'
            if it.thumbnail_url
            else ""
        )
        + f'<div style="font-size:15px;font-weight:600">{escape(_short(it.title))}</div>'
        f'<div style="font-size:14px;color:{_MUTED};margin-top:4px">Schedule the story for: '
        f'<b style="color:{_INK}">{escape(day_clock(it.story_time))}</b></div></td></tr>'
        for it in items
    )
    html = (
        f'<div style="background:{_PAGE};padding:16px"><table role="presentation" width="100%" style="max-width:480px;'
        f'margin:0 auto;background:{_CARD};border-radius:12px;padding:20px;font-family:{_FONT}"><tr><td>'
        f'<div style="font-size:18px;font-weight:700;color:{_INK}">{n} stor{"ies" if n != 1 else "y"} to schedule</div>'
        f'<table role="presentation" width="100%" style="margin-top:12px">{rows}</table>'
        f'<a href="{escape(page_url, quote=True)}" style="display:block;text-align:center;margin-top:18px;padding:14px;'
        f'background:{_ACCENT};color:#fff;border-radius:10px;text-decoration:none;font-weight:600">Get stories</a>'
        f'<div style="font-size:12px;color:{_MUTED};margin-top:10px;text-align:center">Opens a page for 3 days. Open it on your phone.</div>'
        "</td></tr></table></div>"
    )
    return subject, "\n".join(lines), html


def story_card_html(page_url: str, story_when: str) -> str:
    """The card dropped into the trial-graduation email, as a button and a line."""
    return (
        f'<div style="margin:16px 0;padding:14px;border:1px solid {_LINE};border-radius:10px;font-family:{_FONT}">'
        f'<div style="font-size:14px;color:{_INK}"><b>Story for this reel</b></div>'
        f'<div style="font-size:13px;color:{_MUTED};margin:4px 0 10px">Schedule the story for: {escape(story_when)}</div>'
        f'<a href="{escape(page_url, quote=True)}" style="display:inline-block;padding:10px 18px;background:{_ACCENT};'
        'color:#fff;border-radius:8px;text-decoration:none;font-weight:600">Get story</a></div>'
    )


def story_card_text(page_url: str, story_when: str) -> str:
    return f"Story for this reel. Schedule it for: {story_when}\n{page_url}\n"


def _short(title: str, limit: int = 90) -> str:
    one = " ".join(title.split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


# --- the page --------------------------------------------------------------------------------------

_PAGE_CSS = (
    f"body{{margin:0;background:{_PAGE};font-family:{_FONT};color:{_INK}}}"
    ".wrap{max-width:480px;margin:0 auto;padding:16px}"
    f".card{{background:{_CARD};border-radius:14px;padding:16px;margin-bottom:14px}}"
    "h1{font-size:20px;margin:6px 0 14px}h2{font-size:16px;margin:0 0 6px}"
    f".when{{font-size:14px;color:{_MUTED};margin-bottom:10px}}"
    f".sticker{{display:inline-block;background:#fff;border:1px dashed {_MUTED};border-radius:8px;padding:6px 10px;font-size:14px;margin-bottom:12px}}"
    f"button{{width:100%;padding:16px;font-size:17px;font-weight:700;border:0;border-radius:12px;background:{_ACCENT};color:#fff}}"
    "button[disabled]{opacity:.55}"
    f".msg{{font-size:13px;color:{_MUTED};margin-top:8px;min-height:18px}}"
    f".done{{display:flex;align-items:center;gap:8px;margin-top:12px;font-size:15px}}"
    ".done input{width:22px;height:22px}"
    "img.cover{width:64px;border-radius:8px;float:right;margin-left:10px}"
)

# The click handler must stay synchronous up to navigator.share: iOS Safari refuses to share files if an
# await sits between the tap and the call, so the video is fetched into a File as soon as the page loads.
_PAGE_JS = """
const files = {};
function say(id, text) { document.getElementById('msg-' + id).textContent = text; }
async function prepare(card) {
  const id = card.dataset.id, btn = document.getElementById('btn-' + id);
  try {
    const res = await fetch(card.dataset.video);
    if (!res.ok) throw new Error('video ' + res.status);
    files[id] = new File([await res.blob()], card.dataset.name, { type: 'video/mp4' });
    btn.disabled = false; btn.textContent = 'Copy link + save video';
  } catch (e) { btn.textContent = 'Video failed to load. Reload the page.'; }
}
function go(id) {
  const card = document.getElementById('card-' + id), link = card.dataset.link;
  const file = files[id];
  // Copy first (still inside the tap), then hand the file to the share sheet: "Save Video" puts it in Photos.
  const copied = navigator.clipboard ? navigator.clipboard.writeText(link).then(() => true, () => false) : Promise.resolve(false);
  if (file && navigator.canShare && navigator.canShare({ files: [file] })) {
    navigator.share({ files: [file] }).then(
      () => copied.then(ok => say(id, ok ? 'Link copied. Video shared: choose Save Video.' : 'Video shared. Copy the link by hand: ' + link)),
      err => { if (err && err.name !== 'AbortError') say(id, 'Share failed: ' + err.message); else say(id, 'Link copied. Share was cancelled; tap again for the video.'); });
  } else {
    const a = document.createElement('a'); a.href = card.dataset.video; a.download = card.dataset.name; document.body.appendChild(a); a.click(); a.remove();
    copied.then(ok => say(id, ok ? 'Link copied. Video downloading.' : 'Video downloading. Copy the link by hand: ' + link));
  }
}
document.querySelectorAll('.card[data-id]').forEach(prepare);
document.querySelectorAll('.done input').forEach(box => {
  const key = 'story-done-' + box.dataset.id; try { box.checked = localStorage.getItem(key) === '1'; } catch (e) {}
  box.addEventListener('change', () => { try { localStorage.setItem(key, box.checked ? '1' : '0'); } catch (e) {} });
});
"""


def kit_page(token: str, items: list[KitItem]) -> str:
    cards = "".join(_card(token, it) for it in items)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow"><title>Story kit</title>'
        f"<style>{_PAGE_CSS}</style></head><body><div class=\"wrap\"><h1>Stories to schedule</h1>{cards}</div>"
        f"<script>{_PAGE_JS}</script></body></html>"
    )


def _card(token: str, it: KitItem) -> str:
    vid = escape(it.video_id, quote=True)
    base = f"/s/{token}/{vid}"
    cover = f'<img class="cover" src="{base}/cover" alt="">' if it.thumbnail_url else ""
    return (
        f'<div class="card" id="card-{vid}" data-id="{vid}" data-video="{base}/video" data-name="{escape(it.slug, quote=True)}.mp4"'
        f' data-link="{escape(story_link(it.slug), quote=True)}">{cover}'
        f"<h2>{escape(_short(it.title))}</h2>"
        f'<div class="when">Schedule the story for <b>{escape(day_clock(it.story_time))}</b></div>'
        f'<div class="sticker">Sticker text: {escape(STICKER_TEXT)}</div>'
        f'<button id="btn-{vid}" onclick="go(\'{vid}\')" disabled>Preparing video…</button>'
        f'<div class="msg" id="msg-{vid}"></div>'
        f'<label class="done"><input type="checkbox" data-id="{vid}"> Scheduled</label></div>'
    )


def items_from_doc(doc: dict[str, Any]) -> list[KitItem]:
    """Stored kit items back into KitItem objects. On the page, `thumbnail_url` holds the cover's storage key."""
    return [
        KitItem(i["video_id"], i["slug"], i["title"], assume_utc(i["reel_time"]), i.get("thumb_key"))
        for i in doc.get("items", [])
    ]
