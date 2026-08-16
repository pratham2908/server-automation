"""Video source service — browse external S3/R2 buckets and queue imports.

Pure-ish orchestration around :class:`~app.services.r2.R2Service`, which is
already provider-agnostic: R2 and S3 differ only by endpoint and region.

Every boto3 call is blocking, so each one is pushed through ``asyncio.to_thread``
to keep the event loop free.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.models.video_source import SourceBrowseResponse, SourceObject, VideoSourcePublic
from app.services.r2 import R2Service
from app.timezone import now_ist

logger = get_logger(__name__)

# Extensions we consider importable. Anything else in the bucket is ignored so
# the browser does not surface thumbnails, project files, or stray archives.
VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi")

# Queue of source_imports._id values awaiting transfer, drained by the import
# worker. Mirrors the batch_upload_service pattern.
_import_queue: asyncio.Queue[str] | None = None


def get_import_queue() -> asyncio.Queue[str]:
    global _import_queue
    if _import_queue is None:
        _import_queue = asyncio.Queue()
    return _import_queue


def mask_key_id(key_id: str) -> str:
    """Redact the middle of an access key ID for display."""
    if len(key_id) <= 8:
        return "****"
    return f"{key_id[:4]}{'*' * 6}{key_id[-4:]}"


def build_client(source: dict[str, Any]) -> R2Service:
    """Construct an S3 client for *source* (a raw ``video_sources`` document)."""
    return R2Service(
        endpoint_url=source.get("endpoint_url"),
        access_key_id=source["access_key_id"],
        secret_access_key=source["secret_access_key"],
        bucket_name=source["bucket"],
        region_name=source.get("region") or "auto",
    )


def to_public(source: dict[str, Any]) -> VideoSourcePublic:
    """Project a stored source into its API-safe form."""
    return VideoSourcePublic(
        source_id=source["source_id"],
        channel_id=source["channel_id"],
        name=source["name"],
        provider=source["provider"],
        bucket=source["bucket"],
        access_key_id_masked=mask_key_id(source.get("access_key_id", "")),
        endpoint_url=source.get("endpoint_url"),
        region=source.get("region", "auto"),
        prefix=source.get("prefix", ""),
        enabled=source.get("enabled", True),
        last_checked_at=source.get("last_checked_at"),
        last_status=source.get("last_status", "unknown"),
        last_error=source.get("last_error"),
    )


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
            raise ValueError(f"Video source '{source_id}' not found for channel '{channel_id}'")
        return source

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
    # Connection test
    # ------------------------------------------------------------------

    async def test_connection(self, channel_id: str, source_id: str) -> dict[str, Any]:
        """Verify credentials by listing a single object. Records health either way."""
        source = await self._require_source(channel_id, source_id)
        client = build_client(source)

        try:
            page = await asyncio.to_thread(
                client.list_objects_page,
                source.get("prefix", ""),
                "/",
                None,
                1,
            )
        except Exception as exc:
            # Surface the real reason (bad key, wrong bucket, wrong endpoint) —
            # a generic "failed" here costs hours of guessing.
            message = f"{type(exc).__name__}: {exc}"
            await self._record_health(source_id, ok=False, error=message)
            logger.warning("Video source %s connection failed: %s", source_id, message)
            return {"ok": False, "error": message}

        await self._record_health(source_id, ok=True)
        reachable = bool(page["files"] or page["folders"])
        return {
            "ok": True,
            "bucket": source["bucket"],
            "has_content": reachable,
        }

    # ------------------------------------------------------------------
    # Browse
    # ------------------------------------------------------------------

    async def browse(
        self,
        channel_id: str,
        source_id: str,
        prefix: str | None = None,
        cursor: str | None = None,
    ) -> SourceBrowseResponse:
        """List one page of video objects under *prefix*, flagging imported ones."""
        source = await self._require_source(channel_id, source_id)
        root = source.get("prefix", "") or ""

        # An empty prefix means "the source's configured root". Anything else must
        # stay inside that root so a caller cannot browse the whole bucket.
        effective = prefix if prefix else root
        if root and not effective.startswith(root):
            raise ValueError(f"Prefix '{effective}' is outside this source's root '{root}'")

        client = build_client(source)
        try:
            page = await asyncio.to_thread(client.list_objects_page, effective, "/", cursor, 500)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            await self._record_health(source_id, ok=False, error=message)
            raise ValueError(f"Could not list bucket: {message}") from exc

        await self._record_health(source_id, ok=True)

        videos = [f for f in page["files"] if f["key"].lower().endswith(VIDEO_EXTENSIONS)]

        # Flag anything this channel has already pulled in, so the UI can grey it
        # out rather than letting the same file be imported twice.
        keys = [f["key"] for f in videos]
        existing: dict[str, str] = {}
        if keys:
            cursor_docs = self.db.videos.find(
                {"channel_id": channel_id, "source_id": source_id, "source_object_key": {"$in": keys}},
                {"_id": 0, "video_id": 1, "source_object_key": 1},
            )
            async for doc in cursor_docs:
                existing[doc["source_object_key"]] = doc["video_id"]

        return SourceBrowseResponse(
            prefix=effective,
            folders=page["folders"],
            files=[
                SourceObject(
                    key=f["key"],
                    size=f["size"],
                    last_modified=f["last_modified"],
                    imported=f["key"] in existing,
                    video_id=existing.get(f["key"]),
                )
                for f in videos
            ],
            next_cursor=page["next_cursor"],
            is_truncated=page["is_truncated"],
        )

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    async def enqueue_import(
        self,
        channel_id: str,
        source_id: str,
        keys: list[str],
        scheduled_at: str | None = None,
        analyze: bool = True,
    ) -> dict[str, Any]:
        """Create video + import-job records for *keys* and queue them for transfer.

        Video records are created up front in ``uploading`` status so the file
        shows up in the Videos list immediately, mirroring the batch upload flow.
        """
        source = await self._require_source(channel_id, source_id)
        now = now_ist()

        queued: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []

        for key in keys:
            if not key.lower().endswith(VIDEO_EXTENSIONS):
                skipped.append({"key": key, "reason": "not a video file"})
                continue

            already = await self.db.videos.find_one(
                {"channel_id": channel_id, "source_id": source_id, "source_object_key": key},
                {"_id": 0, "video_id": 1},
            )
            if already:
                skipped.append({"key": key, "reason": "already imported"})
                continue

            video_id = str(uuid.uuid4())
            r2_key = f"{channel_id}/{video_id}.mp4"
            filename = key.rsplit("/", 1)[-1]

            await self.db.videos.insert_one(
                {
                    "channel_id": channel_id,
                    "video_id": video_id,
                    "title": filename.rsplit(".", 1)[0],
                    "description": "",
                    "tags": [],
                    "category": "Uncategorized",
                    "status": "uploading",
                    "r2_object_key": r2_key,
                    "packaging_status": "pending",
                    # Provenance — also the dedup key for re-import checks.
                    "source_id": source_id,
                    "source_object_key": key,
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
                    "source_object_key": key,
                    "filename": filename,
                    "video_id": video_id,
                    "r2_key": r2_key,
                    "scheduled_at": scheduled_at,
                    "analyze": analyze,
                    "status": "queued",
                    "message": "Waiting to transfer",
                    "bytes_transferred": 0,
                    "size_bytes": None,
                    "created_at": now,
                    "started_at": None,
                    "completed_at": None,
                }
            )

            get_import_queue().put_nowait(job_id)
            queued.append({"job_id": job_id, "key": key, "video_id": video_id})

        logger.info(
            "Queued %d import(s) from source %s (%s), skipped %d",
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
