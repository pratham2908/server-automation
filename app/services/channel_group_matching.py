"""Work out which channels are the same brand. Pure — no I/O, no database.

Three signals, in descending order of how much they prove:

* **Repost history.** Someone has already copied videos from one channel to
  another by hand. That is a stated relationship, not a guess.
* **Handle.** ``@physicsasmr`` and ``@physicsasmr_official`` are the same brand
  wearing platform-specific suffixes.
* **Name or internal id.** "Geo Ranking" on two platforms; or ``Scroll  and
  tell`` and ``scroll and tell``, which differ only by case and a stray space.

A suggestion is never applied on its own — a wrong link would silently publish a
video to a channel it does not belong on, so a person confirms every one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Platform-specific decoration that says nothing about which brand this is.
_NOISE = ("official", "real", "the", "yt", "ig", "insta", "tv", "hq")

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise(value: str | None) -> str:
    """Casefold and strip punctuation, spacing and an ``@``."""
    if not value:
        return ""
    return _NON_ALNUM.sub("", value.strip().lower().lstrip("@"))


def strip_noise(value: str) -> str:
    """Remove decoration, so ``physicsasmrofficial`` meets ``physicsasmr``."""
    out = value
    for word in _NOISE:
        out = out.replace(word, "")
    return out


@dataclass(slots=True)
class ChannelIdentity:
    """The identifying strings of one channel."""

    channel_id: str
    name: str
    platform: str
    handle: str | None = None

    def keys(self) -> set[str]:
        """Every normalised form this channel could be recognised by."""
        raw = {normalise(self.name), normalise(self.handle), normalise(self.channel_id)}
        stripped = {strip_noise(k) for k in raw}
        # Two characters is not evidence of anything.
        return {k for k in raw | stripped if len(k) >= 3}


@dataclass(slots=True)
class Match:
    """A proposed group, with why it was proposed."""

    channel_ids: list[str] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0


class _Sets:
    """Union-find, so a chain of pairwise matches becomes one group."""

    def __init__(self, items: list[str]) -> None:
        self._parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for item in self._parent:
            out.setdefault(self.find(item), []).append(item)
        return out


def find_matches(
    channels: list[ChannelIdentity],
    repost_pairs: dict[tuple[str, str], int] | None = None,
) -> list[Match]:
    """Group channels that look like the same brand.

    ``repost_pairs`` maps (source, destination) to how many videos have been
    copied between them — the strongest available signal, since a person did it.
    """
    repost_pairs = repost_pairs or {}
    by_id = {c.channel_id: c for c in channels}
    sets = _Sets([c.channel_id for c in channels])

    reasons: dict[frozenset[str], tuple[str, float]] = {}

    for (source, dest), count in repost_pairs.items():
        if source in by_id and dest in by_id and count > 0:
            sets.union(source, dest)
            reasons[frozenset({source, dest})] = (
                f"{count} video{'s' if count != 1 else ''} already reposted between them",
                1.0,
            )

    for i, a in enumerate(channels):
        for b in channels[i + 1 :]:
            shared = a.keys() & b.keys()
            if not shared:
                continue
            sets.union(a.channel_id, b.channel_id)
            key = frozenset({a.channel_id, b.channel_id})
            if key not in reasons:
                reasons[key] = (f"matching name or handle ({sorted(shared)[0]})", 0.75)

    matches: list[Match] = []
    for members in sets.groups().values():
        if len(members) < 2:
            continue
        # Strongest evidence within the group describes the whole group.
        best_reason, best_confidence = "", 0.0
        for i, left in enumerate(members):
            for right in members[i + 1 :]:
                reason, confidence = reasons.get(frozenset({left, right}), ("", 0.0))
                if confidence > best_confidence:
                    best_reason, best_confidence = reason, confidence
        matches.append(
            Match(
                # Ordered so the same input always yields the same suggestion.
                channel_ids=sorted(members),
                reason=best_reason or "matching name or handle",
                confidence=best_confidence,
            )
        )
    return sorted(matches, key=lambda m: (-m.confidence, m.channel_ids))


def pick_primary(members: list[ChannelIdentity]) -> str:
    """Which channel a video should start on.

    YouTube first: it is where long-form originates in every group here, and the
    existing reposts all run YouTube to Instagram. Ties break on channel_id so
    the answer never depends on dict ordering.
    """
    ordered = sorted(members, key=lambda c: (c.platform != "youtube", c.channel_id))
    return ordered[0].channel_id


def group_name(members: list[ChannelIdentity]) -> str:
    """The clearest shared name — the one most members already use."""
    counts: dict[str, int] = {}
    for member in members:
        counts[member.name] = counts.get(member.name, 0) + 1
    # Most common name wins; ties go to the shortest, then alphabetical, so the
    # result is stable rather than dependent on iteration order.
    return sorted(counts, key=lambda n: (-counts[n], len(n), n))[0]
