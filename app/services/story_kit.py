"""The story kit: everything the owner needs to schedule an Instagram story for a reel by hand.

Instagram's API cannot add a link sticker, and stories can only be scheduled in the Meta Business Suite
phone app, so the owner does it on the phone. This module is the pure part: the link, the time, the
token, and the page and email markup. The routes and the database live in ``app/routers/story_kit.py``.

Each channel has ONE story page behind an unguessable token, built live from the reels on every load:
every reel whose story is still to come or went up in the last three days, plus trial reels that have
graduated. Every email links to that same page. Each reel on it is a card with ONE button that, in one
tap, copies the story link and opens the iPhone share sheet with the video file so it can be saved to
Photos (Business Suite's gallery picker cannot see Files, and is not in the share sheet). "Scheduled" is
ticked on the server, so it holds on any device, and ticked reels sink to the bottom.
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
# A reel stays on the page this long after its story time (or its graduation): long enough to catch up
# on a missed one, short enough that a forwarded link only ever exposes a few days of files.
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
    # Ticked "Scheduled" on the page.
    done: bool = False
    # A trial reel that cleared its bar: its story goes up as soon as it is graduated, not at a set time.
    graduated: bool = False

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


def kit_email(items: list[KitItem], page_url: str, reminder: bool = False) -> tuple[str, str, str]:
    """Subject, text and html for one scheduling run. One row per reel, one button to the kit.

    ``reminder`` is the second send, for a story still unticked once its reel is live.
    """
    n = len(items)
    subject = f"Story not scheduled yet: {n} reel{'s' if n != 1 else ''}" if reminder else f"Stories to schedule: {n} reel{'s' if n != 1 else ''}"
    lead = "now live, and the story is not ticked as scheduled yet" if reminder else "just scheduled"
    lines = [f"{n} reel{'s' if n != 1 else ''} {lead}. Schedule a story for each:", ""]
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
    f"h2.section{{font-size:14px;color:{_MUTED};text-transform:uppercase;letter-spacing:.06em;margin:22px 0 10px}}"
    ".card.is-done{opacity:.6}"
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
// Saved on the server so it holds on every device; a failed save is undone so the box never lies.
document.querySelectorAll('.done input').forEach(box => {
  box.addEventListener('change', () => {
    const want = box.checked, id = box.dataset.id;
    fetch(box.dataset.url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ done: want }) })
      .then(res => { if (!res.ok) throw new Error(res.status); document.getElementById('card-' + id).classList.toggle('is-done', want); })
      .catch(() => { box.checked = !want; say(id, 'Could not save that. Try again.'); });
  });
});
"""


def kit_page(token: str, items: list[KitItem]) -> str:
    todo = [it for it in items if not it.done]
    done = [it for it in items if it.done]
    body = "".join(_card(token, it) for it in todo) or '<p class="when">Nothing to schedule right now.</p>'
    if done:
        body += f'<h2 class="section">Scheduled ({len(done)})</h2>' + "".join(_card(token, it) for it in done)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow"><title>Stories to schedule</title>'
        f'<style>{_PAGE_CSS}</style></head><body><div class="wrap"><h1>Stories to schedule</h1>{body}</div>'
        f"<script>{_PAGE_JS}</script></body></html>"
    )


def _card(token: str, it: KitItem) -> str:
    vid = escape(it.video_id, quote=True)
    base = f"/s/{token}/{vid}"
    cover = f'<img class="cover" src="{base}/cover" alt="">' if it.thumbnail_url else ""
    when = (
        "Graduated: schedule the story <b>as soon as you share it to followers</b>"
        if it.graduated
        else f"Schedule the story for <b>{escape(day_clock(it.story_time))}</b>"
    )
    checked = " checked" if it.done else ""
    return (
        f'<div class="card{" is-done" if it.done else ""}" id="card-{vid}" data-id="{vid}" data-video="{base}/video"'
        f' data-name="{escape(it.slug, quote=True)}.mp4" data-link="{escape(story_link(it.slug), quote=True)}">{cover}'
        f"<h2>{escape(_short(it.title))}</h2>"
        f'<div class="when">{when}</div>'
        f'<div class="sticker">Sticker text: {escape(STICKER_TEXT)}</div>'
        f'<button id="btn-{vid}" onclick="go(\'{vid}\')" disabled>Preparing video…</button>'
        f'<div class="msg" id="msg-{vid}"></div>'
        f'<label class="done"><input type="checkbox" data-id="{vid}" data-url="{base}/done"{checked}> Scheduled</label></div>'
    )


def page_items(videos: list[dict[str, Any]], now: datetime) -> list[KitItem]:
    """The reels the channel's story page shows right now, still-to-do first, each group by story time.

    A reel with a story slug is listed until three days after its story time. A trial reel gets no story
    when it posts (it is meant for non-followers) and appears only once it clears its graduation bar,
    for three days from that check. Archived reels are left off.
    """
    cutoff = assume_utc(now) - KIT_LIFETIME
    items: list[KitItem] = []
    for v in videos:
        slug, scheduled = v.get("story_slug"), v.get("scheduled_at")
        if not slug or not isinstance(scheduled, datetime) or v.get("status") == "archived":
            continue
        reel_time = reel_time_ist(scheduled)
        graduated = bool(v.get("trial_graduation")) and v.get("trial_check_state") == "met"
        if v.get("trial_graduation") and not graduated:
            continue
        checked = v.get("trial_check_checked_at")
        anchor = assume_utc(checked) if graduated and isinstance(checked, datetime) else reel_time + STORY_DELAY
        if anchor < cutoff:
            continue
        items.append(
            KitItem(
                video_id=v["video_id"],
                slug=slug,
                title=v.get("title") or slug,
                reel_time=reel_time,
                thumbnail_url=v.get("thumbnail_r2_key"),
                done=v.get("story_scheduled_at") is not None,
                graduated=graduated,
            )
        )
    items.sort(key=lambda it: (it.done, it.story_time))
    return items


# A graduated trial's story goes up as soon as it graduates; the reminder gives that a few hours first.
GRADUATED_REMINDER_DELAY = timedelta(hours=3)


def reminders_due(videos: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Videos whose story is still unticked when it should be done, and not yet reminded about.

    A normal reel is due once it is live; a graduated trial three hours after it graduated. Each gets
    one reminder (``story_reminded_at``), and none after the page has dropped it three days on.
    """
    now = assume_utc(now)
    due = []
    for v in videos:
        scheduled = v.get("scheduled_at")
        if not v.get("story_slug") or not isinstance(scheduled, datetime) or v.get("status") == "archived":
            continue
        if v.get("story_scheduled_at") is not None or v.get("story_reminded_at") is not None:
            continue
        if v.get("trial_graduation"):
            checked = v.get("trial_check_checked_at")
            if v.get("trial_check_state") != "met" or not isinstance(checked, datetime):
                continue
            ready, anchor = assume_utc(checked) + GRADUATED_REMINDER_DELAY, assume_utc(checked)
        else:
            ready = anchor = reel_time_ist(scheduled)
        if ready <= now and anchor + KIT_LIFETIME > now:
            due.append(v)
    return due
