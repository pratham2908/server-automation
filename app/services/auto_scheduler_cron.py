"""Daily auto-scheduler cron.

For every channel that has the auto-scheduler switched on, keep the day's posting
pace: one video per configured slot. Each slot's pass runs an hour before its
time. The decision per slot:

  * If the channel already has enough videos committed today, do nothing.
  * Else take the oldest video from the Ready tab and schedule it for the slot.
  * If Ready is empty, trigger an import from a configured source and re-check a
    while later — once it lands, schedule it the same day.
  * If there is nothing to import either, skip the slot for the day.

At the end of the day a summary of everything scheduled and skipped is emailed.

This is a wall-clock cron built on a short tick loop plus a per-day, per-channel
run document, rather than "sleep until 6 PM": a restart mid-day resumes cleanly,
and each slot's outcome is persisted so nothing is scheduled twice. The pure
decisions (which slots are due, which video to pick) live in
``auto_scheduler_selection``; this module is the side-effecting orchestration.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.config import Settings, get_settings
from app.database import get_channel_platform, not_paused_query
from app.logger import get_logger
from app.services.auto_scheduler_selection import (
    due_slots,
    pending_action_slots,
    pick_ready_video,
    pick_source_video,
    recheck_ready,
    slot_datetimes,
    videos_committed_today,
    wait_exhausted,
)
from app.services.auto_scheduler_summary import format_summary_email
from app.services.email_service import send_email
from app.services.error_reporting import report_error
from app.services.schedule_operation import (
    enqueue_video_for_youtube,
    schedule_single_video_instagram,
)
from app.services.video_source_service import MAX_PAGE_LIMIT, VideoSourceService
from app.timezone import IST, now_ist

logger = get_logger(__name__)

_TICK_SECONDS = 300  # 5 minutes between passes

# Timing defaults; overridable via the ``config`` doc {"key": "auto_scheduler"}.
_DEFAULT_RECHECK_MINUTES = 35
_DEFAULT_MAX_WAIT_MINUTES = 90

# How many source listing pages to walk when choosing a video to import. Bounds a
# runaway catalogue while still seeing enough to apply the oldest/episode rules.
_MAX_CATALOGUE_PAGES = 20

# Slot states persisted on the run document.
_PENDING = "pending"
_SCHEDULED = "scheduled"
_IMPORTING = "importing"
_SKIPPED = "skipped"
_FAILED = "failed"
_TERMINAL = {_SCHEDULED, _SKIPPED, _FAILED}


class _Timing:
    """Resolved timing knobs for one run."""

    def __init__(self, recheck_minutes: int, max_wait_minutes: int) -> None:
        self.recheck_minutes = recheck_minutes
        self.max_wait_minutes = max_wait_minutes


async def _load_timing(db: AsyncIOMotorDatabase) -> _Timing:
    doc = await db.config.find_one({"key": "auto_scheduler"}) or {}
    return _Timing(
        recheck_minutes=int(doc.get("recheck_minutes", _DEFAULT_RECHECK_MINUTES)),
        max_wait_minutes=int(doc.get("max_wait_minutes", _DEFAULT_MAX_WAIT_MINUTES)),
    )


def _day_key(day: date) -> str:
    return day.isoformat()


def _config_of(channel: dict[str, Any]) -> dict[str, Any]:
    return (channel.get("automation_config") or {}).get("auto_scheduler") or {}


def _schedule_times(channel: dict[str, Any]) -> list[str]:
    times = _config_of(channel).get("schedule_times") or []
    # Defensive: keep only well-formed HH:MM strings, in chronological order.
    good = [t for t in times if isinstance(t, str) and ":" in t]
    return sorted(good)


async def _run_doc(db: AsyncIOMotorDatabase, day: date, channel: dict[str, Any]) -> dict[str, Any]:
    """Load, or create, the per-day run document for a channel."""
    channel_id = channel["channel_id"]
    key = _day_key(day)
    existing: dict[str, Any] | None = await db.auto_scheduler_runs.find_one({"date": key, "channel_id": channel_id})
    if existing:
        return existing

    now = now_ist()
    doc = {
        "date": key,
        "channel_id": channel_id,
        "channel_name": channel.get("name") or channel_id,
        "slots": {},
        "created_at": now,
        "updated_at": now,
    }
    # Upsert so two overlapping ticks (shouldn't happen — single task) or a restart
    # cannot create duplicates; the unique index enforces one per (date, channel).
    await db.auto_scheduler_runs.update_one(
        {"date": key, "channel_id": channel_id},
        {"$setOnInsert": doc},
        upsert=True,
    )
    fetched = await db.auto_scheduler_runs.find_one({"date": key, "channel_id": channel_id})
    return fetched or doc


async def _set_slot(
    db: AsyncIOMotorDatabase,
    day: date,
    channel_id: str,
    slot: str,
    patch: dict[str, Any],
) -> None:
    fields = {f"slots.{slot}.{k}": v for k, v in patch.items()}
    fields["updated_at"] = now_ist()
    await db.auto_scheduler_runs.update_one(
        {"date": _day_key(day), "channel_id": channel_id},
        {"$set": fields},
    )


def _slot_states(run_doc: dict[str, Any]) -> dict[str, str]:
    slots = run_doc.get("slots") or {}
    return {slot: (data or {}).get("state", _PENDING) for slot, data in slots.items()}


async def _channel_videos_today(db: AsyncIOMotorDatabase, channel_id: str, day: date) -> list[dict[str, Any]]:
    """Videos that count toward today's pace: published or scheduled/queued for today."""
    day_start = datetime.combine(day, time.min, tzinfo=IST)
    cursor = db.videos.find(
        {
            "channel_id": channel_id,
            "$or": [
                {"status": "published", "published_at": {"$gte": day_start}},
                {"status": {"$in": ["scheduled", "queued"]}, "scheduled_at": {"$gte": day_start}},
            ],
        }
    )
    return await cursor.to_list(length=None)


async def _ready_videos(db: AsyncIOMotorDatabase, channel_id: str) -> list[dict[str, Any]]:
    return await db.videos.find({"channel_id": channel_id, "status": "ready"}).to_list(length=None)


async def _enqueue_for_channel(
    db: AsyncIOMotorDatabase,
    channel: dict[str, Any],
    video_doc: dict[str, Any],
    schedule_at: datetime,
) -> dict[str, Any]:
    """Schedule one video on one channel via the platform-appropriate path.

    Deliberately group-unaware: ``_schedule_video`` layers the linked-channel
    expansion on top, and the expansion calls this so it cannot recurse.
    """
    channel_id = channel["channel_id"]
    if get_channel_platform(channel) == "instagram":
        return await schedule_single_video_instagram(
            db=db, channel_id=channel_id, video_doc=video_doc, scheduled_at=schedule_at
        )
    return await enqueue_video_for_youtube(db=db, channel_id=channel_id, video_doc=video_doc, scheduled_at=schedule_at)


async def _schedule_linked_siblings(
    db: AsyncIOMotorDatabase,
    channel: dict[str, Any],
    video_doc: dict[str, Any],
    schedule_at: datetime,
) -> list[dict[str, Any]]:
    """Schedule this video's sibling records on the channels linked to this one.

    Linking says "same brand, same content". The import already creates the
    sibling record and analysis makes it postable, but nothing ever scheduled it,
    so a linked channel accumulated ready videos and posted none of them.

    Only *this video's* siblings are touched — the records sharing its
    ``multi_channel_group_id``. Scheduling whatever else happened to be ready on
    the linked channel would post unrelated content at the primary's slot.

    Every outcome is returned, including the misses, so the daily summary can say
    a linked channel was skipped rather than quietly omitting it.
    """
    from app.services.channel_group_service import ChannelGroupService

    group_id = video_doc.get("multi_channel_group_id")
    if not group_id:
        return []

    targets = await ChannelGroupService(db).expansion_targets(channel["channel_id"])
    if not targets:
        return []

    outcomes: list[dict[str, Any]] = []
    for target_id in targets:
        target = await db.channels.find_one({"channel_id": target_id})
        if not target:
            continue  # unlinked or deleted between import and slot — nothing to post to

        entry: dict[str, Any] = {"channel_id": target_id, "channel_name": target.get("name") or target_id}
        sibling = await db.videos.find_one({"channel_id": target_id, "multi_channel_group_id": group_id})

        if not sibling:
            entry.update(state=_SKIPPED, reason="no linked copy of this video on that channel")
        elif sibling.get("status") != "ready":
            # Usually still analysing: packaging writes its title, and a video is
            # not postable before that lands. Reported, not retried — the next
            # video's slot will find it ready.
            entry.update(state=_SKIPPED, reason=f"linked copy is '{sibling.get('status')}', not ready")
        else:
            result = await _enqueue_for_channel(db, target, sibling, schedule_at)
            if result.get("status") == "queued":
                entry.update(state=_SCHEDULED, video_id=sibling.get("video_id"))
                logger.info(
                    "Auto-scheduler: also scheduled linked channel %s for %s",
                    target_id,
                    channel["channel_id"],
                )
            else:
                entry.update(state=_FAILED, reason=f"schedule failed: {result.get('status')}")
        outcomes.append(entry)

    return outcomes


async def _schedule_video(
    db: AsyncIOMotorDatabase,
    channel: dict[str, Any],
    video_doc: dict[str, Any],
    schedule_at: datetime,
) -> dict[str, Any]:
    """Schedule a ready video, and its linked copies, at the same time."""
    result = await _enqueue_for_channel(db, channel, video_doc, schedule_at)
    if result.get("status") != "queued":
        return result
    linked = await _schedule_linked_siblings(db, channel, video_doc, schedule_at)
    return {**result, "linked": linked} if linked else result


async def _pick_import_across_sources(
    service: VideoSourceService,
    channel_id: str,
) -> tuple[str, str, str] | None:
    """Choose a video to import for a channel.

    Returns ``(source_id, source_name, source_video_id)`` for the first enabled
    source that yields a pick under its own rules (GeoRank oldest / VidForge
    episode rules), or ``None`` when nothing is importable anywhere.
    """
    sources = await service.list_sources(channel_id)
    for source in sources:
        if not source.enabled:
            continue
        catalogue: dict[str, Any] = {}
        cursor: str | None = None
        for _ in range(_MAX_CATALOGUE_PAGES):
            try:
                page = await service.list_videos(channel_id, source.source_id, MAX_PAGE_LIMIT, cursor)
            except (ConnectionError, LookupError) as exc:
                logger.warning("Auto-scheduler: source %s unreadable, skipping: %s", source.source_id, exc)
                break
            for v in page.videos:
                catalogue[v.id] = v
            if not page.next_cursor:
                break
            cursor = page.next_cursor

        pick = pick_source_video(list(catalogue.values()))
        if pick is not None:
            return source.source_id, source.name, pick.id
    return None


def _slot_schedule_at(slot: str, day: date, now: datetime) -> datetime:
    """When to schedule a video for ``slot`` — the slot time, or just ahead of now
    if that has already passed (e.g. an import landed late), kept on the same day."""
    _run_at, schedule_at = slot_datetimes(slot, day)
    return max(schedule_at, now + timedelta(minutes=2))


async def _recheck_imports(
    db: AsyncIOMotorDatabase,
    channel: dict[str, Any],
    run_doc: dict[str, Any],
    day: date,
    now: datetime,
    timing: _Timing,
) -> None:
    """Phase B: for slots awaiting an import, schedule once ready or give up."""
    channel_id = channel["channel_id"]
    slots = run_doc.get("slots") or {}
    for slot, data in slots.items():
        if (data or {}).get("state") != _IMPORTING:
            continue
        awaiting_since = data.get("awaiting_since")
        if not isinstance(awaiting_since, datetime):
            continue
        if awaiting_since.tzinfo is None:
            awaiting_since = awaiting_since.replace(tzinfo=IST)
        if not recheck_ready(awaiting_since, now, timing.recheck_minutes):
            continue  # too soon to look again

        video_id = data.get("video_id")
        video = await db.videos.find_one({"channel_id": channel_id, "video_id": video_id}) if video_id else None
        status = (video or {}).get("status")

        if status == "ready" and video is not None:
            result = await _schedule_video(db, channel, video, _slot_schedule_at(slot, day, now))
            if result.get("status") == "queued":
                await _set_slot(db, day, channel_id, slot, {"state": _SCHEDULED, "linked": result.get("linked")})
                logger.info("Auto-scheduler: imported video for %s slot %s is ready and scheduled", channel_id, slot)
            else:
                await _set_slot(
                    db, day, channel_id, slot, {"state": _FAILED, "reason": f"schedule failed: {result.get('status')}"}
                )
        elif status in ("queued", "scheduled", "published"):
            # Already scheduled by some other path while we waited.
            await _set_slot(db, day, channel_id, slot, {"state": _SCHEDULED})
        elif wait_exhausted(awaiting_since, now, timing.max_wait_minutes):
            await _set_slot(db, day, channel_id, slot, {"state": _FAILED, "reason": "import not ready in time"})
            logger.warning("Auto-scheduler: import for %s slot %s did not become ready in time", channel_id, slot)
        # else: still processing — leave it to recheck next tick.


async def _fill_pending_slots(
    db: AsyncIOMotorDatabase,
    channel: dict[str, Any],
    service: VideoSourceService,
    day: date,
    now: datetime,
) -> None:
    """Phase A: for slots still needing a video, schedule from Ready or import."""
    channel_id = channel["channel_id"]
    schedule_times = _schedule_times(channel)

    run_doc = await _run_doc(db, day, channel)
    committed = videos_committed_today(await _channel_videos_today(db, channel_id, day), day)
    to_fill = pending_action_slots(schedule_times, now, committed, _slot_states(run_doc))
    if not to_fill:
        return

    ready_pool = await _ready_videos(db, channel_id)
    used_ids: set[str] = set()

    for slot in to_fill:
        # Ensure the slot exists as pending before we act on it.
        await _set_slot(db, day, channel_id, slot, {"state": _PENDING})

        ready = pick_ready_video([v for v in ready_pool if v.get("video_id") not in used_ids])
        if ready is not None:
            result = await _schedule_video(db, channel, ready, _slot_schedule_at(slot, day, now))
            ready_id = ready.get("video_id")
            if result.get("status") == "queued":
                if ready_id:
                    used_ids.add(ready_id)
                await _set_slot(
                    db,
                    day,
                    channel_id,
                    slot,
                    {"state": _SCHEDULED, "video_id": ready_id, "linked": result.get("linked")},
                )
                logger.info("Auto-scheduler: scheduled ready video for %s slot %s", channel_id, slot)
            else:
                await _set_slot(
                    db, day, channel_id, slot, {"state": _FAILED, "reason": f"schedule failed: {result.get('status')}"}
                )
            continue

        # Ready is empty — trigger an import and re-check later.
        pick = await _pick_import_across_sources(service, channel_id)
        if pick is None:
            await _set_slot(db, day, channel_id, slot, {"state": _SKIPPED, "reason": "no videos available to import"})
            logger.info("Auto-scheduler: nothing to schedule or import for %s slot %s", channel_id, slot)
            continue

        source_id, source_name, source_video_id = pick
        enqueued = await service.enqueue_import(channel_id, source_id, [source_video_id])
        queued = enqueued.get("queued") or []
        if queued:
            await _set_slot(
                db,
                day,
                channel_id,
                slot,
                {
                    "state": _IMPORTING,
                    "video_id": queued[0]["video_id"],
                    "source": source_name,
                    "awaiting_since": now,
                },
            )
            logger.info("Auto-scheduler: triggered import from %s for %s slot %s", source_name, channel_id, slot)
        else:
            reason = (enqueued.get("skipped") or [{}])[0].get("reason", "import could not be queued")
            await _set_slot(db, day, channel_id, slot, {"state": _SKIPPED, "reason": reason})


async def process_channel(
    db: AsyncIOMotorDatabase,
    channel: dict[str, Any],
    service: VideoSourceService,
    day: date,
    now: datetime,
    timing: _Timing,
) -> None:
    """One tick's work for one channel: recheck imports, then fill due slots."""
    if not _schedule_times(channel):
        return  # enabled but no slots configured — nothing to do
    run_doc = await _run_doc(db, day, channel)
    await _recheck_imports(db, channel, run_doc, day, now, timing)
    await _fill_pending_slots(db, channel, service, day, now)


# ------------------------------------------------------------------
# End-of-day summary
# ------------------------------------------------------------------


def _all_slots_terminal(channels: list[dict[str, Any]], run_docs: dict[str, dict[str, Any]], now: datetime) -> bool:
    """True when every enabled channel's every slot has reached a terminal state
    and its time has passed — i.e. the day's work is finished."""
    saw_a_slot = False
    for channel in channels:
        times = _schedule_times(channel)
        if not times:
            continue
        states = _slot_states(run_docs.get(channel["channel_id"], {}))
        for slot in times:
            saw_a_slot = True
            _run_at, schedule_at = slot_datetimes(slot, now.date())
            if now < schedule_at:
                return False  # a slot's time has not arrived yet
            if states.get(slot, _PENDING) not in _TERMINAL:
                return False
    return saw_a_slot


def _assemble_summary(day: date, run_docs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    scheduled: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for doc in run_docs.values():
        name = doc.get("channel_name") or doc.get("channel_id")
        for slot, data in (doc.get("slots") or {}).items():
            state = (data or {}).get("state")
            if state == _SCHEDULED:
                scheduled.append(
                    {
                        "channel_id": doc.get("channel_id"),
                        "channel_name": name,
                        "slot": slot,
                        "video_id": (data or {}).get("video_id", ""),
                        "source": (data or {}).get("source"),
                    }
                )
            elif state in (_SKIPPED, _FAILED):
                skipped.append(
                    {
                        "channel_id": doc.get("channel_id"),
                        "channel_name": name,
                        "slot": slot,
                        "reason": (data or {}).get("reason", state),
                    }
                )

            # Linked channels post from the primary's slot, so they have no slot
            # row of their own. Without this they would post silently — the email
            # would say one channel was scheduled while two actually were.
            for link in (data or {}).get("linked") or []:
                row = {
                    "channel_id": link.get("channel_id"),
                    "channel_name": link.get("channel_name") or link.get("channel_id"),
                    "slot": slot,
                    "linked_to": name,
                }
                if link.get("state") == _SCHEDULED:
                    scheduled.append({**row, "video_id": link.get("video_id", ""), "source": f"linked to {name}"})
                else:
                    skipped.append({**row, "reason": link.get("reason", link.get("state", _SKIPPED))})

    return {"date": _day_key(day), "scheduled": scheduled, "skipped": skipped}


async def _enrich_summary(db: AsyncIOMotorDatabase, summary: dict[str, Any]) -> dict[str, Any]:
    """Add the things a person actually reads: the channel's picture and the title.

    The run docs carry ids, which is right for the machine and useless in an
    inbox — nobody gains anything from a uuid. This resolves each one to a title,
    and each channel to its avatar, in two queries rather than one per row.
    """
    entries = [*summary.get("scheduled", []), *summary.get("skipped", [])]
    if not entries:
        return summary

    channel_ids = {e.get("channel_id") for e in entries if e.get("channel_id")}
    thumbnails = {
        doc["channel_id"]: doc.get("thumbnail_url")
        for doc in await db.channels.find(
            {"channel_id": {"$in": sorted(channel_ids)}}, {"_id": 0, "channel_id": 1, "thumbnail_url": 1}
        ).to_list(None)
    }

    video_ids = {e.get("video_id") for e in entries if e.get("video_id")}
    titles = {
        doc["video_id"]: doc.get("title")
        for doc in await db.videos.find(
            {"video_id": {"$in": sorted(video_ids)}}, {"_id": 0, "video_id": 1, "title": 1}
        ).to_list(None)
    }

    for entry in entries:
        entry["channel_thumbnail"] = thumbnails.get(entry.get("channel_id"))
        if entry.get("video_id"):
            # A video deleted between scheduling and the summary leaves no title;
            # the formatter shows "Untitled" rather than falling back to the id.
            entry["video_title"] = titles.get(entry["video_id"])
    return summary


async def _resolve_recipient(db: AsyncIOMotorDatabase, settings: Settings) -> str | None:
    if settings.SUMMARY_EMAIL_TO:
        return settings.SUMMARY_EMAIL_TO
    profile = await db.profiles.find_one({}, {"email": 1})
    return (profile or {}).get("email")


async def _maybe_send_summary(
    db: AsyncIOMotorDatabase,
    settings: Settings,
    channels: list[dict[str, Any]],
    day: date,
    now: datetime,
) -> None:
    """Send the day's summary exactly once, after all channels are done."""
    run_docs: dict[str, dict[str, Any]] = {}
    for channel in channels:
        doc = await db.auto_scheduler_runs.find_one({"date": _day_key(day), "channel_id": channel["channel_id"]})
        if doc:
            run_docs[channel["channel_id"]] = doc

    if not _all_slots_terminal(channels, run_docs, now):
        return

    key = _day_key(day)
    # Ensure the latch doc exists, then atomically flip it. The tick that wins the
    # flip (match on sent=False) is the only one that sends; every later tick sees
    # no match and returns. Kept as two steps so we never upsert against a
    # non-matching filter (which would collide with the unique date index).
    await db.auto_scheduler_summaries.update_one(
        {"date": key},
        {"$setOnInsert": {"date": key, "sent": False}},
        upsert=True,
    )
    won = await db.auto_scheduler_summaries.find_one_and_update(
        {"date": key, "sent": False},
        {"$set": {"sent": True, "sent_at": now}},
        return_document=False,
    )
    if won is None:
        return  # already latched and sent by an earlier tick

    summary = await _enrich_summary(db, _assemble_summary(day, run_docs))
    email = format_summary_email(summary)
    recipient = await _resolve_recipient(db, settings)
    logger.info(
        "Auto-scheduler day complete: %d scheduled, %d skipped",
        len(summary["scheduled"]),
        len(summary["skipped"]),
    )
    await send_email(settings, recipient, email.subject, email.text, email.html)


# ------------------------------------------------------------------
# Tick loop
# ------------------------------------------------------------------


async def _tick(db: AsyncIOMotorDatabase) -> None:
    now = now_ist()
    day = now.date()
    settings = get_settings()
    timing = await _load_timing(db)
    service = VideoSourceService(db)

    channels = await db.channels.find({"automation_config.auto_scheduler.enabled": True, **not_paused_query()}).to_list(
        length=None
    )
    if not channels:
        return

    # Only do per-channel work once a slot is actually due, but always evaluate the
    # end-of-day summary so it fires even on a quiet day.
    for channel in channels:
        if due_slots(_schedule_times(channel), now):
            await process_channel(db, channel, service, day, now, timing)

    await _maybe_send_summary(db, settings, channels, day, now)


async def run_auto_scheduler(db: AsyncIOMotorDatabase) -> None:
    """Infinite tick loop for the daily auto-scheduler."""
    logger.info("Auto-scheduler cron started (tick interval: %ds)", _TICK_SECONDS)
    while True:
        try:
            await _tick(db)
        except asyncio.CancelledError:
            logger.info("Auto-scheduler cron shutting down")
            break
        except Exception as exc:
            logger.exception("Auto-scheduler tick failed")
            await report_error(
                feature="Auto-scheduler: tick",
                message=f"Auto-scheduler tick error: {exc!s}",
                exception=exc,
            )
        await asyncio.sleep(_TICK_SECONDS)
