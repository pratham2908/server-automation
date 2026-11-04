"""Comment-to-DM numbers for a period: DMs per hour or day, and how far people got after the DM.

Pure: rows in, numbers out. The router reads ``comment_dm_events`` (one per matching comment), the
person's own messages and taps after their DM, and the stored follow checks, and hands them here.

The funnel counts PEOPLE, not comments: commented → got the DM → wrote back → tapped one of our
buttons or chips → follows us. "Follows us" is only known for people Instagram lets us ask about —
those who wrote to us or tapped something; it is never known for someone who only commented.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel

from app.timezone import IST, assume_utc

Window = Literal["24h", "7d", "30d", "all"]
Bucket = Literal["hour", "day"]

WINDOW_SPAN: dict[Window, timedelta | None] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}
TOP_REELS = 8


class Point(BaseModel):
    # Bucket start, IST ISO.
    at: str
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    queued: int = 0


class Funnel(BaseModel):
    comments: int
    people: int
    dm_people: int
    wrote_back: int
    tapped: int
    # Of the people who got the DM: how many Instagram let us ask about, and how many of those follow us.
    follow_known: int
    following: int


class ReelNumbers(BaseModel):
    media_id: str
    title: str | None = None
    comments: int
    sent: int
    failed: int
    skipped: int


class Numbers(BaseModel):
    window: Window
    bucket: Bucket
    points: list[Point]
    funnel: Funnel
    # Comment to DM, for DMs sent in the period.
    median_seconds_to_dm: float | None
    fail_rate: float | None
    reels: list[ReelNumbers]


def _ist(value: Any) -> datetime | None:
    return assume_utc(value).astimezone(IST) if isinstance(value, datetime) else None


def bucket_start(at: datetime, bucket: Bucket) -> datetime:
    local = at.astimezone(IST)
    if bucket == "hour":
        return local.replace(minute=0, second=0, microsecond=0)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def bucket_for(window: Window) -> Bucket:
    return "hour" if window == "24h" else "day"


def _buckets(start: datetime, now: datetime, bucket: Bucket) -> list[datetime]:
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    out, at = [], bucket_start(start, bucket)
    end = bucket_start(now, bucket)
    while at <= end:
        out.append(at)
        # Adding a day in IST is safe (no DST); re-flooring keeps hour buckets on the hour.
        at = bucket_start(at + step, bucket)
    return out


def _status_key(status: str) -> str | None:
    if status == "sent":
        return "sent"
    if status == "failed":
        return "failed"
    if status == "skipped":
        return "skipped"
    if status in ("pending", "sending"):
        return "queued"
    return None


def numbers(
    events: Iterable[Mapping[str, Any]],
    *,
    window: Window,
    now: datetime,
    replies_by_person: Mapping[str, list[datetime]],
    taps_by_person: Mapping[str, list[datetime]],
    follows_by_person: Mapping[str, bool | None],
) -> Numbers:
    """The period's numbers. *replies_by_person* / *taps_by_person*: when each person (by Instagram id)
    wrote to us / tapped something. *follows_by_person*: the stored follow check, None where unknown."""
    span = WINDOW_SPAN[window]
    bucket = bucket_for(window)
    since = now - span if span is not None else None
    rows = []
    for e in events:
        created = _ist(e.get("created_at"))
        if created is None or (since is not None and created < since):
            continue
        rows.append((created, e))
    start = since if since is not None else min((c for c, _ in rows), default=now)
    points = {b: Point(at=b.isoformat()) for b in _buckets(start, now, bucket)}
    reels: dict[str, ReelNumbers] = {}
    people: set[str] = set()
    dm_people: dict[str, datetime] = {}
    waits: list[float] = []
    for created, e in rows:
        status = str(e.get("status") or "")
        key = _status_key(status)
        point = points.get(bucket_start(created, bucket))
        if point is not None and key is not None:
            setattr(point, key, getattr(point, key) + 1)
        media = str(e.get("media_id") or "")
        reel = reels.setdefault(media, ReelNumbers(media_id=media, comments=0, sent=0, failed=0, skipped=0))
        reel.comments += 1
        if status in ("sent", "failed", "skipped"):
            setattr(reel, status, getattr(reel, status) + 1)
        person = str(e.get("from_id") or "") or str(e.get("person") or "")
        if person:
            people.add(person)
        sent_at = _ist(e.get("sent_at"))
        if status == "sent" and person and sent_at is not None:
            # Their first DM in the period is the one anything after it answers.
            dm_people[person] = min(sent_at, dm_people.get(person, sent_at))
            commented = _ist(e.get("commented_at"))
            if commented is not None:
                waits.append((sent_at - commented).total_seconds())

    def after_dm(by_person: Mapping[str, list[datetime]]) -> int:
        return sum(
            1
            for person, sent_at in dm_people.items()
            if any((t := _ist(at)) is not None and t >= sent_at for at in by_person.get(person, []))
        )

    known = [follows_by_person.get(p) for p in dm_people if follows_by_person.get(p) is not None]
    sent = sum(1 for _, e in rows if e.get("status") == "sent")
    failed = sum(1 for _, e in rows if e.get("status") == "failed")
    top = sorted(reels.values(), key=lambda r: r.comments, reverse=True)[:TOP_REELS]
    return Numbers(
        window=window,
        bucket=bucket,
        points=list(points.values()),
        funnel=Funnel(
            comments=len(rows),
            people=len(people),
            dm_people=len(dm_people),
            wrote_back=after_dm(replies_by_person),
            tapped=after_dm(taps_by_person),
            follow_known=len(known),
            following=sum(1 for k in known if k),
        ),
        median_seconds_to_dm=statistics.median(waits) if waits else None,
        fail_rate=failed / (sent + failed) if sent + failed else None,
        reels=top,
    )
