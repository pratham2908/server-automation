"""Exact per-reel follows, read from the table Instagram's Business Suite shows.

Instagram's API refuses ``follows`` for reels, but the Business Suite content table lists
it per reel. That table is the only exact source, so it is copied in by hand: this module
turns the pasted text into (publish time, follows) rows and pairs each with a stored reel
by its publish time, which the table shows to the minute.

Business Suite's CSV export carries the same lifetime count with the post's media id, so ``parse_csv``
rows match exactly, with no publish-time pairing.

Pure apart from ``apply_follow_rows`` and ``apply_csv_rows``, the database writes.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.instagram_content_analysis import parse_published_at
from app.timezone import IST

FOLLOWS_SOURCE = "business_suite_export"
CSV_SOURCE = "business_suite_csv"
# The table prints publish times to the minute and ours come from the platform's own timestamp.
MATCH_TOLERANCE = timedelta(minutes=3)

_DATE = re.compile(r"^(\d{1,2}) ([A-Za-z]+) (\d{1,2}):(\d{2})$")
_COUNT = re.compile(r"^\d[\d,]*$")
_MONTHS = {
    name: i
    for i, name in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        1,
    )
}


@dataclass(frozen=True)
class FollowRow:
    published: datetime  # IST, to the minute
    follows: int


@dataclass(frozen=True)
class ImportResult:
    matched: int
    unmatched: list[FollowRow]


def _parse_date(line: str, today: date) -> datetime | None:
    found = _DATE.match(line)
    month = _MONTHS.get(found.group(2).lower()) if found else None
    if not found or month is None:
        return None
    day, hour, minute = int(found.group(1)), int(found.group(3)), int(found.group(4))
    # The table prints no year; a date still in the future must be last year's.
    for year in (today.year, today.year - 1):
        try:
            when = datetime(year, month, day, hour, minute, tzinfo=IST)
        except ValueError:
            return None
        if when.date() <= today + timedelta(days=1):
            return when
    return None


def parse_paste(text: str, today: date) -> list[FollowRow]:
    """One row per reel in the pasted table.

    Each reel's block holds the line ``Reel``, the account, the publish time, then the
    columns in order with follows first. Anything that doesn't fit that shape is skipped
    rather than guessed at, and a cut-off final block still yields its follows if it got that far.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    rows: list[FollowRow] = []
    for i, line in enumerate(lines):
        if line != "Reel" or i + 3 >= len(lines):
            continue
        published = _parse_date(lines[i + 2], today)
        follows = lines[i + 3]
        if published is not None and _COUNT.match(follows):
            rows.append(FollowRow(published, int(follows.replace(",", ""))))
    return rows


def match_rows(
    rows: Sequence[FollowRow], reels: Mapping[str, datetime | None]
) -> tuple[dict[str, int], list[FollowRow]]:
    """Follows by media id, and the rows no reel was published within a few minutes of.

    Each reel takes at most one row, the nearest in time, so two reels posted minutes
    apart can't both claim the same line.
    """
    follows_by_media: dict[str, int] = {}
    unmatched: list[FollowRow] = []
    for row in sorted(rows, key=lambda r: r.published):
        candidates = [
            (abs(published - row.published), media_id)
            for media_id, published in reels.items()
            if published is not None
            and media_id not in follows_by_media
            and abs(published - row.published) <= MATCH_TOLERANCE
        ]
        if candidates:
            follows_by_media[min(candidates)[1]] = row.follows
        else:
            unmatched.append(row)
    return follows_by_media, unmatched


async def apply_follow_rows(
    db: AsyncIOMotorDatabase, channel_id: str, rows: Sequence[FollowRow], today: date
) -> ImportResult:
    """Store each matched reel's exact follows on its video, marked with where it came from."""
    reels: dict[str, datetime | None] = {}
    async for video in db.videos.find(
        {"channel_id": channel_id, "instagram_media_id": {"$ne": None}}, {"instagram_media_id": 1, "published_at": 1}
    ):
        reels[video["instagram_media_id"]] = parse_published_at(video.get("published_at"))
    matched, unmatched = match_rows(rows, reels)
    for media_id, follows in matched.items():
        await db.videos.update_one(
            {"channel_id": channel_id, "instagram_media_id": media_id},
            {
                "$set": {
                    "metadata.follows": follows,
                    "metadata.follows_source": FOLLOWS_SOURCE,
                    "metadata.follows_as_of": today.isoformat(),
                }
            },
        )
    return ImportResult(matched=len(matched), unmatched=unmatched)


# ---- The CSV export ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class CsvFollowRow:
    media_id: str
    username: str  # the account it was posted on, lowercase
    follows: int
    views: int | None


@dataclass(frozen=True)
class CsvImportResult:
    matched: dict[str, int]  # per account username
    not_ours: dict[str, list[str]]  # media ids we hold no video for, per account (hand-posted, or older)


def _count(raw: str | None) -> int | None:
    text = (raw or "").replace(",", "").strip()
    return int(text) if text.isdigit() else None


def parse_csv(text: str) -> list[CsvFollowRow]:
    """One row per post in a Business Suite content export ("Post ID", "Account username", "Follows", ...).

    Only lifetime rows count (the export can also be split by day). A row without a post id or a follows
    number is skipped rather than read as zero. If a post appears twice, the higher count is kept: the
    export is a lifetime total, so a lower one is an older reading.
    """
    rows: dict[str, CsvFollowRow] = {}
    for rec in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        media_id = (rec.get("Post ID") or "").strip()
        follows = _count(rec.get("Follows"))
        if not media_id or follows is None or (rec.get("Date") or "Lifetime").strip() != "Lifetime":
            continue
        row = CsvFollowRow(
            media_id, (rec.get("Account username") or "").strip().lower(), follows, _count(rec.get("Views"))
        )
        if media_id not in rows or rows[media_id].follows < follows:
            rows[media_id] = row
    return list(rows.values())


async def apply_csv_rows(db: AsyncIOMotorDatabase, rows: Sequence[CsvFollowRow], as_of: date) -> CsvImportResult:
    """Store each row's follows on our video with that media id, on the channel of the row's account.

    The account is matched by its Instagram username, so one export covering several accounts fills each.
    """
    channels: dict[str, str] = {}
    async for ch in db.channels.find(
        {"instagram_username": {"$nin": [None, ""]}}, {"channel_id": 1, "instagram_username": 1}
    ):
        channels.setdefault(str(ch["instagram_username"]).lower(), str(ch["channel_id"]))
    matched: dict[str, int] = {}
    not_ours: dict[str, list[str]] = {}
    for row in rows:
        channel_id = channels.get(row.username)
        found = None
        if channel_id:
            found = await db.videos.update_one(
                {"channel_id": channel_id, "instagram_media_id": row.media_id},
                {
                    "$set": {
                        "metadata.follows": row.follows,
                        "metadata.follows_source": CSV_SOURCE,
                        "metadata.follows_as_of": as_of.isoformat(),
                    }
                },
            )
        if found is not None and found.matched_count:
            matched[row.username] = matched.get(row.username, 0) + 1
        else:
            not_ours.setdefault(row.username, []).append(row.media_id)
    return CsvImportResult(matched=matched, not_ours=not_ours)
