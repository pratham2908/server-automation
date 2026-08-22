"""Video source service — browse a channel's content app and queue imports.

Apps do not share a contract: one is cursor-paginated behind a static secret,
another is page-numbered behind an account login. All of that lives in the
per-kind adapters under :mod:`app.services.video_sources`; this module holds the
part that is the same whatever the app is — dedup, job records, the queue, and
health tracking — and never branches on kind.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import ValidationError

from app.logger import get_logger
from app.models.video_source import (
    COMPLETED_STATUS,
    SourceCreateRequest,
    SourceListResponse,
    SourceVideo,
    VideoSource,
    VideoSourcePublic,
)
from app.services.video_sources import adapter_for, describe_http_error
from app.timezone import now_ist

logger = get_logger(__name__)

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100

# Bounded catalogue walk when resolving ids to import: enough to cover any
# realistic selection without letting one request page an app forever.
MAX_CATALOGUE_PAGES = 10

# Queue of source_imports job ids awaiting transfer, drained by the import
# worker. Mirrors the batch_upload_service pattern.
_import_queue: asyncio.Queue[str] | None = None


def get_import_queue() -> asyncio.Queue[str]:
    global _import_queue
    if _import_queue is None:
        _import_queue = asyncio.Queue()
    return _import_queue


def parse_source(doc: dict[str, Any]) -> VideoSource:
    """Validate a stored source document into its typed model.

    Sources written before kinds existed have their settings flat on the document
    instead of under ``config``. Those fail loudly here rather than being coerced,
    because guessing a kind for a document that never declared one is exactly how
    one channel's credentials end up being sent to another channel's app.
    """
    try:
        return VideoSource(**doc)
    except ValidationError as exc:
        if "config" in doc:
            raise
        raise ValueError(
            f"Source '{doc.get('source_id')}' predates typed configs and has no 'config' — "
            "run scripts/migrate_video_sources.py to convert it"
        ) from exc


def to_public(source: VideoSource) -> VideoSourcePublic:
    """Project a source into its API-safe form.

    Credentials are asked of the adapter as a redacted hint; the raw config never
    reaches this object, so no serialisation path can leak one.
    """
    adapter = adapter_for(source)
    return VideoSourcePublic(
        source_id=source.source_id,
        channel_id=source.channel_id,
        name=source.name,
        kind=source.kind,
        base_url=source.base_url,
        list_path=source.config.list_path,
        credential_hint=adapter.credential_hint(source),
        supports_mark_imported=adapter.supports_mark_imported(source),
        enabled=source.enabled,
        last_checked_at=source.last_checked_at,
        last_status=source.last_status,
        last_error=source.last_error,
    )


class VideoSourceService:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    async def list_sources(self, channel_id: str) -> list[VideoSourcePublic]:
        docs = await self.db.video_sources.find({"channel_id": channel_id}).sort("created_at", 1).to_list(length=None)
        sources: list[VideoSourcePublic] = []
        for doc in docs:
            try:
                sources.append(to_public(parse_source(doc)))
            except (ValidationError, ValueError) as exc:
                # One malformed source must not blank the whole panel for a channel
                # that has other, working ones.
                logger.error("Skipping unreadable video source %s: %s", doc.get("source_id"), exc)
        return sources

    async def create_source(self, channel_id: str, payload: SourceCreateRequest) -> VideoSourcePublic:
        """Register a content app for a channel, after proving it answers.

        The app is called before anything is written. A source whose credentials
        are wrong is worse than no source: it looks configured, fails on every
        browse, and the only way to correct it from here would be another write.
        Refusing to store one means the form is always the place to fix a typo.
        """
        channel = await self.db.channels.find_one({"channel_id": channel_id}, {"_id": 1})
        if not channel:
            raise LookupError(f"No channel '{channel_id}' exists")

        clash = await self.db.video_sources.find_one({"channel_id": channel_id, "name": payload.name})
        if clash:
            raise FileExistsError(f"This channel already has a source named '{payload.name}'")

        source = VideoSource(
            source_id=str(uuid.uuid4()),
            channel_id=channel_id,
            name=payload.name,
            base_url=payload.base_url,
            config=payload.config,
        )

        try:
            await adapter_for(source).probe(source)
        except Exception as exc:
            raise ConnectionError(describe_http_error(exc)) from exc

        doc = source.model_dump()
        doc["last_checked_at"] = now_ist()
        doc["last_status"] = "ok"
        await self.db.video_sources.insert_one(doc)

        logger.info(
            "Registered %s source '%s' (%s) for channel %s",
            source.kind,
            source.name,
            source.source_id,
            channel_id,
        )
        # Reloaded through the same projection every other read uses, so there is
        # one path to the wire and it cannot carry a credential.
        return to_public(source)

    async def load_source(self, source_id: str) -> VideoSource:
        """Look a source up by id alone — for the worker, which holds a job."""
        doc = await self.db.video_sources.find_one({"source_id": source_id})
        if not doc:
            raise LookupError(f"Video source '{source_id}' not found")
        return parse_source(doc)

    async def _require_source(self, channel_id: str, source_id: str) -> VideoSource:
        doc = await self.db.video_sources.find_one({"channel_id": channel_id, "source_id": source_id})
        if not doc:
            raise LookupError(f"Video source '{source_id}' not found for channel '{channel_id}'")
        return parse_source(doc)

    async def _record_health(self, source_id: str, ok: bool, error: str | None = None) -> None:
        await self.db.video_sources.update_one(
            {"source_id": source_id},
            {
                "$set": {
                    "last_checked_at": now_ist(),
                    "last_status": "ok" if ok else "error",
                    "last_error": None if ok else error,
                    "updated_at": now_ist(),
                }
            },
        )

    # ------------------------------------------------------------------
    # App calls — all delegated to the source's adapter
    # ------------------------------------------------------------------

    async def test_connection(self, channel_id: str, source_id: str) -> dict[str, Any]:
        """Verify credentials by asking the app for a single video."""
        source = await self._require_source(channel_id, source_id)
        try:
            result = await adapter_for(source).probe(source)
        except Exception as exc:
            message = describe_http_error(exc)
            await self._record_health(source_id, ok=False, error=message)
            logger.warning("Video source %s connection failed: %s", source_id, message)
            return {"ok": False, "kind": source.kind, "error": message}

        await self._record_health(source_id, ok=True)
        return {"kind": source.kind, **result}

    async def download_url(self, source: VideoSource, source_video_id: str) -> str:
        """A freshly minted download URL, for the worker at transfer time."""
        return await adapter_for(source).fetch_download_url(source, source_video_id)

    async def mark_sent(self, channel_id: str, source_id: str, source_video_id: str) -> dict[str, Any]:
        """Tell the app it has already delivered a video, on an operator's say-so.

        The reason this exists: an app can only report delivery for videos it
        pushed to us. Anything the channel published another way still looks
        unsent, so it keeps being offered for import — and importing it would
        republish something already live. Marking retires it at the source, where
        every future listing will see it, rather than in a local ignore-list only
        this system would know about.

        If we happen to hold the video, its id goes along so the app can link the
        two; when we do not, the mark stands on its own.
        """
        source = await self._require_source(channel_id, source_id)
        adapter = adapter_for(source)
        if not adapter.supports_mark_imported(source):
            return {"ok": False, "error": f"'{source.name}' has no endpoint for marking a delivery"}

        existing = await self.db.videos.find_one(
            {"channel_id": channel_id, "source_id": source_id, "source_video_id": source_video_id},
            {"_id": 0, "video_id": 1},
        )
        our_video_id = existing["video_id"] if existing else None

        error = await self.notify_imported(source, source_video_id, our_video_id)
        if error:
            logger.warning("Manual mark-sent failed for %s on %s: %s", source_video_id, source_id, error)
            return {"ok": False, "error": error}

        logger.info("Marked %s as already sent on %s (%s)", source_video_id, source.name, source_id)
        return {"ok": True, "video_id": our_video_id}

    async def notify_imported(self, source: VideoSource, source_video_id: str, our_video_id: str | None) -> str | None:
        """Tell the app we ingested a video, closing the pull-model loop.

        Without this the app still believes the video was never delivered and may
        later push it through the external upload API, producing a second copy of
        something we already hold — our dedup cannot catch that, because a push
        carries no ``source_video_id``.

        Returns None on success or a description of the failure; it never raises,
        so a bad callback cannot destroy an otherwise-good import.
        """
        try:
            return await adapter_for(source).mark_imported(source, source_video_id, our_video_id)
        except Exception as exc:
            return describe_http_error(exc)

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    async def list_videos(
        self,
        channel_id: str,
        source_id: str,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
    ) -> SourceListResponse:
        """One page of the app's finished videos, flagging ones already here."""
        source = await self._require_source(channel_id, source_id)

        try:
            page = await adapter_for(source).fetch_page(source, limit, cursor)
        except Exception as exc:
            message = describe_http_error(exc)
            await self._record_health(source_id, ok=False, error=message)
            raise ConnectionError(message) from exc

        await self._record_health(source_id, ok=True)
        await self._mark_already_present(channel_id, source_id, page.videos)

        return SourceListResponse(
            videos=page.videos,
            next_cursor=page.next_cursor,
            url_ttl_seconds=page.url_ttl_seconds,
        )

    async def _mark_already_present(
        self,
        channel_id: str,
        source_id: str,
        videos: list[SourceVideo],
    ) -> None:
        """Flag videos this channel already has, from either delivery route.

        Two ways a video can already be here:

        * we pulled it ourselves, so it carries our ``source_video_id``; or
        * the app pushed it through the external upload API, in which case we hold
          no source fields at all and only the app's ``externalVideoId`` — which is
          our own ``video_id`` — connects the two.

        Checking only the first would let a pushed video be imported again as a
        silent duplicate, so both are resolved here. An app that reports no
        ``externalVideoId`` simply gets the first check, which is still correct —
        it just cannot detect its own pushes.
        """
        if not videos:
            return

        by_source_key: dict[str, str] = {}
        docs = self.db.videos.find(
            {"channel_id": channel_id, "source_id": source_id, "source_video_id": {"$in": [v.id for v in videos]}},
            {"_id": 0, "video_id": 1, "source_video_id": 1},
        )
        async for doc in docs:
            by_source_key[doc["source_video_id"]] = doc["video_id"]

        # Only trust an externalVideoId that really resolves to a video on this
        # channel — a stale id from another environment must not mask a video.
        claimed = [v.external_video_id for v in videos if v.external_video_id and v.id not in by_source_key]
        pushed: set[str] = set()
        if claimed:
            docs = self.db.videos.find(
                {"channel_id": channel_id, "video_id": {"$in": claimed}},
                {"_id": 0, "video_id": 1},
            )
            async for doc in docs:
                pushed.add(doc["video_id"])

        for v in videos:
            if v.id in by_source_key:
                v.imported = True
                v.video_id = by_source_key[v.id]
                v.delivered_by = "import"
            elif v.external_video_id and v.external_video_id in pushed:
                v.imported = True
                v.video_id = v.external_video_id
                v.delivered_by = "push"

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    async def enqueue_import(
        self,
        channel_id: str,
        source_id: str,
        video_ids: list[str],
        scheduled_at: str | None = None,
        analyze: bool = True,
    ) -> dict[str, Any]:
        """Create video + import-job records for *video_ids* and queue the transfers.

        The download URL is deliberately *not* resolved here — the worker mints a
        fresh one at transfer time, so a backed-up queue can never outlive it.
        """
        source = await self._require_source(channel_id, source_id)
        now = now_ist()

        # Walking the listing gives titles and statuses for everything being
        # imported, so the video records start out with the app's real metadata.
        catalogue: dict[str, SourceVideo] = {}
        cursor: str | None = None
        for _ in range(MAX_CATALOGUE_PAGES):
            page = await self.list_videos(channel_id, source_id, MAX_PAGE_LIMIT, cursor)
            for v in page.videos:
                catalogue[v.id] = v
            if not page.next_cursor or all(vid in catalogue for vid in video_ids):
                break
            cursor = page.next_cursor

        queued: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []

        for source_video_id in video_ids:
            meta = catalogue.get(source_video_id)
            if meta is None:
                skipped.append({"id": source_video_id, "reason": "not found in the app's catalogue"})
                continue
            if meta.status != COMPLETED_STATUS:
                skipped.append({"id": source_video_id, "reason": f"render status is '{meta.status}'"})
                continue
            if meta.imported:
                reason = (
                    "already in the system — the app pushed it via the external API"
                    if meta.delivered_by == "push"
                    else "already imported"
                )
                skipped.append({"id": source_video_id, "reason": reason})
                continue

            video_id = str(uuid.uuid4())
            r2_key = f"{channel_id}/{video_id}.mp4"

            await self.db.videos.insert_one(
                {
                    "channel_id": channel_id,
                    "video_id": video_id,
                    "title": meta.title,
                    "description": "",
                    "tags": [],
                    "category": "Uncategorized",
                    "status": "uploading",
                    "r2_object_key": r2_key,
                    "packaging_status": "pending",
                    # Provenance — also the dedup key for re-import checks.
                    "source_id": source_id,
                    "source_video_id": source_video_id,
                    "created_at": now,
                    "updated_at": now,
                }
            )

            job_id = str(uuid.uuid4())
            await self.db.source_imports.insert_one(
                {
                    "job_id": job_id,
                    "channel_id": channel_id,
                    "source_id": source_id,
                    "source_video_id": source_video_id,
                    "title": meta.title,
                    "video_id": video_id,
                    "r2_key": r2_key,
                    "scheduled_at": scheduled_at,
                    "analyze": analyze,
                    "status": "queued",
                    "message": "Waiting to transfer",
                    "size_bytes": meta.size_bytes,
                    "notified_at": None,
                    "notify_error": None,
                    "created_at": now,
                    "started_at": None,
                    "completed_at": None,
                }
            )

            get_import_queue().put_nowait(job_id)
            queued.append({"job_id": job_id, "source_video_id": source_video_id, "video_id": video_id})

        logger.info(
            "Queued %d import(s) from %s (%s, kind=%s), skipped %d",
            len(queued),
            source.name,
            source_id,
            source.kind,
            len(skipped),
        )
        return {"ok": True, "queued": queued, "skipped": skipped}

    async def list_imports(self, channel_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Recent import jobs for a channel, newest first."""
        docs = (
            await self.db.source_imports.find({"channel_id": channel_id}, {"_id": 0})
            .sort("created_at", -1)
            .limit(limit)
            .to_list(length=limit)
        )
        for d in docs:
            for field in ("created_at", "started_at", "completed_at", "notified_at"):
                if d.get(field) is not None:
                    d[field] = d[field].isoformat()
        return docs
