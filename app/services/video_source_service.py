"""Video source service — read a channel app's export API and queue imports.

Every channel app implements the same contract::

    GET {base_url}{list_path}?limit=&cursor=   -> {videos: [...], nextCursor, urlTtlSeconds}
    GET {base_url}{detail_path with {id}}      -> one video, freshly minted downloadUrl

so a single client covers all of them; only host, paths, and secret vary per
channel and all three live on the source document.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.models.video_source import (
    COMPLETED_STATUS,
    SourceListResponse,
    SourceVideo,
    VideoSourcePublic,
)
from app.timezone import now_ist

logger = get_logger(__name__)

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
REQUEST_TIMEOUT_S = 30.0

# Queue of source_imports._id values awaiting transfer, drained by the import
# worker. Mirrors the batch_upload_service pattern.
_import_queue: asyncio.Queue[str] | None = None


def get_import_queue() -> asyncio.Queue[str]:
    global _import_queue
    if _import_queue is None:
        _import_queue = asyncio.Queue()
    return _import_queue


def mask_secret(secret: str) -> str:
    """Show only enough of the shared secret to tell two of them apart."""
    if len(secret) <= 4:
        return "****"
    return f"****{secret[-4:]}"


def auth_headers(source: dict[str, Any]) -> dict[str, str]:
    """Build the auth header this app expects."""
    secret = source["api_key"]
    if source.get("auth_style") == "api_key_header":
        return {"X-Api-Key": secret}
    return {"Authorization": f"Bearer {secret}"}


def to_public(source: dict[str, Any]) -> VideoSourcePublic:
    """Project a stored source into its API-safe form."""
    return VideoSourcePublic(
        source_id=source["source_id"],
        channel_id=source["channel_id"],
        name=source["name"],
        base_url=source["base_url"],
        list_path=source.get("list_path", "/api/ext/videos"),
        auth_style=source.get("auth_style", "bearer"),
        api_key_masked=mask_secret(source.get("api_key", "")),
        enabled=source.get("enabled", True),
        last_checked_at=source.get("last_checked_at"),
        last_status=source.get("last_status", "unknown"),
        last_error=source.get("last_error"),
    )


def normalise_video(raw: dict[str, Any]) -> SourceVideo:
    """Map one app-shaped video object onto our snake_case model."""
    return SourceVideo(
        id=str(raw["id"]),
        title=raw.get("title") or "Untitled",
        status=raw.get("status") or "unknown",
        duration_ms=raw.get("durationMs"),
        created_at=raw.get("createdAt"),
        thumbnail_url=raw.get("thumbnailUrl"),
        content_type=raw.get("contentType") or "video/mp4",
        already_sent_to_channel=bool(raw.get("alreadySentToChannel", False)),
        external_video_id=raw.get("externalVideoId"),
    )


def describe_http_error(exc: Exception) -> str:
    """Turn a transport or status failure into something worth reading in the UI."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return f"HTTP {code} — the app rejected our credentials"
        if code == 404:
            return f"HTTP {code} — endpoint not found (check the configured path)"
        body = exc.response.text[:200].strip()
        return f"HTTP {code}{f' — {body}' if body else ''}"
    if isinstance(exc, httpx.TimeoutException):
        return f"Timed out after {REQUEST_TIMEOUT_S:.0f}s"
    if isinstance(exc, httpx.RequestError):
        return f"Could not reach the app: {exc}"
    return f"{type(exc).__name__}: {exc}"


class VideoSourceService:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    async def list_sources(self, channel_id: str) -> list[VideoSourcePublic]:
        docs = await self.db.video_sources.find({"channel_id": channel_id}).sort("created_at", 1).to_list(length=None)
        return [to_public(d) for d in docs]

    async def _require_source(self, channel_id: str, source_id: str) -> dict[str, Any]:
        source = await self.db.video_sources.find_one({"channel_id": channel_id, "source_id": source_id})
        if not source:
            raise LookupError(f"Video source '{source_id}' not found for channel '{channel_id}'")
        return source  # type: ignore

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
    # App calls
    # ------------------------------------------------------------------

    async def _fetch_page(self, source: dict[str, Any], limit: int, cursor: str | None) -> dict[str, Any]:
        """GET one page from the app's list endpoint."""
        url = f"{source['base_url']}{source.get('list_path', '/api/ext/videos')}"
        params: dict[str, Any] = {"limit": min(limit, MAX_PAGE_LIMIT)}
        if cursor:
            params["cursor"] = cursor

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            resp = await client.get(url, params=params, headers=auth_headers(source))
            resp.raise_for_status()
            return resp.json()  # type: ignore

    async def fetch_download_url(self, source: dict[str, Any], video_id: str) -> dict[str, Any]:
        """GET one video from the app, with a freshly minted ``downloadUrl``."""
        path = source.get("detail_path", "/api/ext/videos/{id}").replace("{id}", video_id)
        url = f"{source['base_url']}{path}"

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            resp = await client.get(url, headers=auth_headers(source))
            resp.raise_for_status()
            data = resp.json()

        # The detail endpoint may return the video bare or wrapped; accept both
        # so a harmless shape difference between apps is not a hard failure.
        return data.get("video", data)  # type: ignore

    # ------------------------------------------------------------------
    # Connection test
    # ------------------------------------------------------------------

    async def test_connection(self, channel_id: str, source_id: str) -> dict[str, Any]:
        """Verify credentials by asking the app for a single video."""
        source = await self._require_source(channel_id, source_id)
        try:
            data = await self._fetch_page(source, limit=1, cursor=None)
        except Exception as exc:
            message = describe_http_error(exc)
            await self._record_health(source_id, ok=False, error=message)
            logger.warning("Video source %s connection failed: %s", source_id, message)
            return {"ok": False, "error": message}

        await self._record_health(source_id, ok=True)
        return {
            "ok": True,
            "base_url": source["base_url"],
            "has_content": bool(data.get("videos")),
            "url_ttl_seconds": data.get("urlTtlSeconds"),
        }

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
        """One page of the app's finished videos, flagging ones already imported."""
        source = await self._require_source(channel_id, source_id)

        try:
            data = await self._fetch_page(source, limit, cursor)
        except Exception as exc:
            message = describe_http_error(exc)
            await self._record_health(source_id, ok=False, error=message)
            raise ConnectionError(message) from exc

        await self._record_health(source_id, ok=True)

        videos = [normalise_video(v) for v in data.get("videos", []) if v.get("id")]

        await self._mark_already_present(channel_id, source_id, videos)

        return SourceListResponse(
            videos=videos,
            next_cursor=data.get("nextCursor"),
            url_ttl_seconds=data.get("urlTtlSeconds"),
        )

    async def _mark_already_present(
        self,
        channel_id: str,
        source_id: str,
        videos: list[SourceVideo],
    ) -> None:
        """Flag renders this channel already has, from either delivery route.

        Two ways a render can already be here:

        * we pulled it ourselves, so it carries our ``source_video_id``; or
        * the app pushed it through the external upload API, in which case we hold
          no source fields at all and only the app's ``externalVideoId`` — which is
          our own ``video_id`` — connects the two.

        Checking only the first would let a pushed render be imported again as a
        silent duplicate, so both are resolved here.
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
        # channel — a stale id from another environment must not mask a render.
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

        The presigned URL is deliberately *not* resolved here — the worker mints a
        fresh one at transfer time, so a backed-up queue can never outlive it.
        """
        source = await self._require_source(channel_id, source_id)
        now = now_ist()

        # One list call gives titles and statuses for everything being imported,
        # so the video records start out with the app's real metadata.
        catalogue: dict[str, SourceVideo] = {}
        cursor: str | None = None
        for _ in range(10):  # bounded: 10 pages x 100 = 1000 most recent renders
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
                    "size_bytes": None,
                    "created_at": now,
                    "started_at": None,
                    "completed_at": None,
                }
            )

            get_import_queue().put_nowait(job_id)
            queued.append({"job_id": job_id, "source_video_id": source_video_id, "video_id": video_id})

        logger.info(
            "Queued %d import(s) from %s (%s), skipped %d",
            len(queued),
            source["name"],
            source_id,
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
            for field in ("created_at", "started_at", "completed_at"):
                if d.get(field) is not None:
                    d[field] = d[field].isoformat()
        return docs
