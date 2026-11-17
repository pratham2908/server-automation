"""Per-reel numbers the Instagram app shows but the API won't give, read off a screen recording.

The owner records the phone while opening each reel's "Reel insights" (Overview, Engagement and
Audience tabs). On-device OCR (``scripts/app_insights_ocr.swift``) turns the frames into lines of
``x,y|text`` (normalised, y from the top) under a ``### <frame>`` header; this module reads those into
one record per reel and pairs each with a stored reel. Profile visits, bio link clicks, the
follower/non-follower split, viewer age and country, and where the views came from (Reels tab,
Feed, Explore...) per reel exist only there.

Pure: no I/O. ``app_insights_store`` writes the result.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_NUM = re.compile(r"^\d[\d,.]*\s?[KkMm]?$")
_PCT = re.compile(r"^\d+(?:\.\d+)?%$")
_LINE = re.compile(r"^([\d.]+),([\d.]+)\|(.*)$")
# A country name: words of letters (accents too), no digits or periods, so not one of the app's sentences.
_COUNTRY = re.compile(r"^(?=.{3,32}$)[^\W\d_]+(?:[ '-][^\W\d_]+)*$")

# Rows on the Engagement tab: label on the left, value at the right edge of the same row.
_ROW_FIELDS = {
    "profilevisits": "profile_visits",
    "follows": "follows",
    "biolinkclicks": "bio_link_clicks",
    "likes": "likes",
    "comments": "comments",
    "reposts": "reposts",
    "shares": "shares",
    "saves": "saves",
}
# Overview tiles: label with its value right under it.
_TILE_FIELDS = {"views": "views", "viewers": "viewers", "follows": "follows"}
_ICONS = ("likes", "comments", "reposts", "shares", "saves")  # the row of counts under the thumbnail
AGES = ("13-17", "18-24", "25-34", "35-44", "45-54", "55-64", "65+")
# "Top sources of views" on the Overview tab: laid out like the country rows, so named outright.
SOURCES = ("Reels tab", "Feed", "Explore", "Profile", "Stories", "Search", "Audio page", "Hashtags", "Other")
# The OCR sometimes reads the app's K and M as their Cyrillic lookalikes ("3К").
_LOOKALIKES = str.maketrans({"К": "K", "к": "k", "М": "M", "м": "m"})
_NOT_COUNTRIES = {
    "followers",
    "nonfollowers",
    "age",
    "country",
    "gender",
    "all",
    "audiencedetails",
    "whoviewedyourreel",
    "summary",
    "overview",
    "engagement",
    "audience",
    *_ROW_FIELDS,
    *_TILE_FIELDS,
}

Token = tuple[float, float, str]  # x, y, text


@dataclass
class ReelInsights:
    """What one reel's insights screens showed. Counts the app rounds ("7K") stay rounded."""

    frames: list[str] = field(default_factory=list)
    thumbnail_text: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    shown: dict[str, str] = field(default_factory=dict)  # each count as the app printed it
    avg_watch_seconds: int | None = None
    followers_pct: float | None = None
    non_followers_pct: float | None = None
    age_pct: dict[str, float] = field(default_factory=dict)
    country_pct: dict[str, float] = field(default_factory=dict)
    gender_pct: dict[str, float] = field(default_factory=dict)
    source_pct: dict[str, float] = field(default_factory=dict)  # share of views per surface

    def as_doc(self) -> dict[str, Any]:
        return {
            "counts": self.counts,
            "shown": self.shown,
            "avg_watch_seconds": self.avg_watch_seconds,
            "viewers_followers_pct": self.followers_pct,
            "viewers_non_followers_pct": self.non_followers_pct,
            "age_pct": self.age_pct,
            "country_pct": self.country_pct,
            "gender_pct": self.gender_pct,
            "source_pct": self.source_pct,
        }


def parse_count(text: str) -> int | None:
    """``"1,129,593"`` → 1129593, ``"7K"`` → 7000, ``"1.1 M"`` → 1100000; None if it isn't a count."""
    raw = text.replace(",", "").replace(" ", "")
    if not raw or not _NUM.match(raw):
        return None
    scale = {"k": 1_000, "m": 1_000_000}.get(raw[-1].lower(), 1)
    try:
        return round(float(raw.rstrip("KkMm")) * scale)
    except ValueError:
        return None


def parse_ocr(text: str) -> list[tuple[str, list[Token]]]:
    """``[(frame name, [(x, y, text)])]`` from the OCR tool's output."""
    frames: list[tuple[str, list[Token]]] = []
    for line in text.splitlines():
        if line.startswith("### "):
            frames.append((line[4:].strip(), []))
            continue
        found = _LINE.match(line)
        if found and frames:
            frames[-1][1].append((float(found[1]), float(found[2]), found[3].strip().translate(_LOOKALIKES)))
    return frames


def _squash(text: str) -> str:
    """A label as a lookup key: "Bio link clicks" and the OCR's "Biolinkclicks" both read "biolinkclicks"."""
    return re.sub(r"[^a-z]", "", text.lower())


def _same_row_value(tokens: Sequence[Token], y: float) -> str | None:
    found = [(x, t) for x, yy, t in tokens if abs(yy - y) < 0.015 and x > 0.6 and _NUM.match(t.replace(" ", ""))]
    return max(found)[1] if found else None


def _tile_value(tokens: Sequence[Token], x: float, y: float) -> str | None:
    found = [(yy - y, t) for xx, yy, t in tokens if 0.012 < yy - y < 0.05 and abs(xx - x) < 0.07]
    return min(found)[1] if found else None


def _percent_after(tokens: Sequence[Token], y: float) -> float | None:
    """Audience rows print the label, then its percentage just below at the right edge."""
    found = [(yy - y, t) for xx, yy, t in tokens if 0.004 < yy - y < 0.04 and xx > 0.6 and _PCT.match(t)]
    return float(min(found)[1].rstrip("%")) if found else None


def _seconds(text: str) -> int | None:
    found = re.match(r"^(?:(\d+)m\s*)?(\d+)s$", text.replace(" ", ""))
    return int(found[1] or 0) * 60 + int(found[2]) if found else None


def _read_frame(tokens: Sequence[Token], votes: defaultdict[str, Counter[str]]) -> None:
    icons = sorted((x, t) for x, y, t in tokens if _NUM.match(t) and _icon_row(tokens, y))
    if len(icons) == len(_ICONS):
        for name, (_, t) in zip(_ICONS, icons, strict=True):
            votes["icon:" + name][t] += 1
    for x, y, text in tokens:
        key = _squash(text)
        if key in _ROW_FIELDS and x < 0.3 and (value := _same_row_value(tokens, y)):
            votes[_ROW_FIELDS[key]][value] += 1
        if key in _TILE_FIELDS and x < 0.6 and (value := _tile_value(tokens, x, y)) and _NUM.match(value):
            votes["tile:" + _TILE_FIELDS[key]][value] += 1
        if key == "averagewatchtime" and (value := _tile_value(tokens, x, y)):
            votes["avg_watch"][value] += 1
        if x > 0.3:
            continue
        pct = _percent_after(tokens, y)
        if pct is None:
            continue
        if key in ("followers", "nonfollowers"):
            votes["viewers:" + key][str(pct)] += 1
        elif text in AGES:
            votes["age:" + text][str(pct)] += 1
        elif text in ("Men", "Women", "Male", "Female"):
            votes["gender:" + text.lower()][str(pct)] += 1
        elif text in SOURCES:
            votes["source:" + text][str(pct)] += 1
        # A country name: no sentence of the app's own text ("Rates are listed in order ...").
        # Letters may be accented: Instagram writes "Türkiye".
        elif _COUNTRY.match(text) and text[0].isupper() and not text.isupper() and key not in _NOT_COUNTRIES:
            votes["country:" + text][str(pct)] += 1


def _icon_row(tokens: Sequence[Token], y: float) -> bool:
    """The counts row sits right above the Overview / Engagement / Audience tabs."""
    return any(_squash(t) == "overview" and 0.02 < ty - y < 0.08 for _, ty, t in tokens)


def read_reels(frames: Iterable[tuple[str, Sequence[Token]]]) -> list[ReelInsights]:
    """One record per run of "Reel insights" frames; runs showing the same counts row are one reel."""
    runs: list[tuple[list[str], defaultdict[str, Counter[str]], Counter[str]]] = []
    open_run = False
    for name, tokens in frames:
        if not any(t.startswith("Reel insight") for _, _, t in tokens):
            open_run = False
            continue
        if not open_run:
            runs.append(([], defaultdict(Counter), Counter()))
            open_run = True
        names, votes, thumb = runs[-1]
        names.append(name)
        _read_frame(tokens, votes)
        for _, y, t in tokens:
            if 0.12 < y < 0.42 and t.isupper() and len(t) >= 4 and parse_count(t) is None:
                thumb[t] += 1
    merged: list[tuple[list[str], defaultdict[str, Counter[str]], Counter[str]]] = []
    for run in runs:
        if merged and _icons(merged[-1][1]) and _icons(merged[-1][1]) == _icons(run[1]):
            merged[-1][0].extend(run[0])
            for key, counter in run[1].items():
                merged[-1][1][key].update(counter)
            merged[-1][2].update(run[2])
        else:
            merged.append(run)
    return [_record(names, votes, thumb) for names, votes, thumb in merged if _icons(votes) or votes]


def _icons(votes: Mapping[str, Counter[str]]) -> tuple[str, ...]:
    return tuple(votes["icon:" + n].most_common(1)[0][0] for n in _ICONS if votes.get("icon:" + n))


def _record(names: list[str], votes: Mapping[str, Counter[str]], thumb: Counter[str]) -> ReelInsights:
    best = {key: counter.most_common(1)[0][0] for key, counter in votes.items() if counter}
    rec = ReelInsights(frames=names, thumbnail_text=[t for t, _ in thumb.most_common(3)])
    for name in _ICONS:
        _set_count(rec, name, best.get(name) or best.get("icon:" + name))
    for name in ("profile_visits", "bio_link_clicks"):
        _set_count(rec, name, best.get(name))
    _set_count(rec, "follows", best.get("follows") or best.get("tile:follows"))
    _set_count(rec, "views", best.get("tile:views"))
    _set_count(rec, "viewers", best.get("tile:viewers"))
    rec.avg_watch_seconds = _seconds(best.get("avg_watch", ""))
    rec.followers_pct = _pct(best.get("viewers:followers"))
    rec.non_followers_pct = _pct(best.get("viewers:nonfollowers"))
    for key, value in best.items():
        kind, _, label = key.partition(":")
        target = {
            "age": rec.age_pct,
            "country": rec.country_pct,
            "gender": rec.gender_pct,
            "source": rec.source_pct,
        }.get(kind)
        if target is not None and (pct := _pct(value)) is not None:
            target[label] = pct
    rec.age_pct = {age: rec.age_pct[age] for age in AGES if age in rec.age_pct}
    rec.country_pct = dict(sorted(rec.country_pct.items(), key=lambda kv: -kv[1]))
    rec.source_pct = dict(sorted(rec.source_pct.items(), key=lambda kv: -kv[1]))
    return rec


def _set_count(rec: ReelInsights, name: str, shown: str | None) -> None:
    if shown is not None and (value := parse_count(shown)) is not None:
        rec.counts[name] = value
        rec.shown[name] = shown


def _pct(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def match_reel(
    rec: ReelInsights, reels: Sequence[Mapping[str, Any]], tolerance: float = 0.02, drift: float = 0.10
) -> str | None:
    """The media id of the stored reel these insights are for, or None when it isn't clear.

    The view count picks the reel. Ours differs from the app by however long the two reads were
    apart: one reel within ``tolerance`` is taken. Past that (a new reel gains several percent in an
    hour, and a reel of similar reach can sit close by), reels within ``drift`` are candidates, the
    like count must agree, and a thumbnail text must name exactly one of them. Without a view count,
    the like count and the thumbnail text decide alone.
    """

    def near(ours: Any, theirs: int | None, slack: float) -> bool:
        return isinstance(ours, int | float) and theirs is not None and abs(ours - theirs) <= max(5, theirs * slack)

    def likes_agree(reel: Mapping[str, Any]) -> bool:
        # "1K" or "17.9K" is rounded to a tenth of the unit: allow that on top of the growth.
        shown = rec.shown.get("likes", "").upper()
        rounding = 100 if shown.endswith("K") else 100_000 if shown.endswith("M") else 0
        likes = rec.counts.get("likes")
        ours = reel.get("likes")
        return (
            isinstance(ours, int | float) and likes is not None and abs(ours - likes) <= rounding + max(5, likes * 0.06)
        )

    def named_by_thumbnail(hits: list[Mapping[str, Any]]) -> str | None:
        # Most-read text first. The series name ("4 FRIENDS MEET") is in every title, so only a text
        # that names exactly one candidate (the city) settles it.
        for text in rec.thumbnail_text:
            named = [r for r in hits if text.lower() in str(r.get("title") or "").lower()]
            if len(named) == 1:
                return str(named[0]["media_id"])
        return None

    views = rec.counts.get("views")
    if views:
        tight = [r for r in reels if near(r.get("views"), views, tolerance)]
        if len(tight) == 1:
            return str(tight[0]["media_id"])
        reels = [r for r in reels if near(r.get("views"), views, drift)]
    return named_by_thumbnail([r for r in reels if likes_agree(r)])
