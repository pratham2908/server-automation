import asyncio
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime
from typing import Any

from dateutil.parser import isoparse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.config import get_settings
from app.database import (
    get_channel_platform,
    get_content_schema_for_prompt,
    update_channel_task_status,
)
from app.logger import get_logger
from app.services.downloader import (
    download_instagram_media_to_r2,
    download_youtube_video_to_r2,
)
from app.services.error_reporting import create_monitored_task, report_error
from app.services.r2 import R2Service
from app.services.retention_analysis import run_retention_analysis
from app.services.schedule_operation import (
    enqueue_video_for_youtube,
    schedule_single_video_instagram,
)
from app.services.scheduler import compute_schedule_slots
from app.services.todo_engine import recompute_category
from app.timezone import IST, now_ist, to_ist_iso

logger = get_logger(__name__)


class VideoService:
    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        r2_service: R2Service | None = None,
        gemini_service: Any = None,
        youtube_manager: Any = None,
        instagram_manager: Any = None,
    ):
        self.db = db
        self.r2 = r2_service
        self.gemini = gemini_service
        self.youtube_manager = youtube_manager
        self.instagram_manager = instagram_manager

    # --- Helper methods ---
    async def _r2_refcount(self, r2_key: str) -> int:
        """Count how many video records share this R2 key."""
        return await self.db.videos.count_documents({"r2_object_key": r2_key})

    async def _safe_delete_r2(self, r2_key: str) -> None:
        """Delete from R2 only if no other video record references the same key."""
        if not self.r2 or not r2_key:
            return
        if await self._r2_refcount(r2_key) <= 1:
            try:
                self.r2.delete_video(r2_key)
            except Exception:
                pass

    async def verify_video_file(self, channel_id: str, video_id: str) -> bool:
        """Check if a video has a valid file in R2."""
        video = await self.db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
        if not video or not video.get("r2_object_key"):
            return False
        if self.r2:
            try:
                return self.r2.file_exists(video["r2_object_key"])
            except Exception:
                return False
        return True

    async def _get_youtube_service(self, channel_id: str):
        if not self.youtube_manager:
            return None
        return await self.youtube_manager.get_service(channel_id)

    async def _get_instagram_service(self, channel_id: str):
        if not self.instagram_manager:
            return None
        return await self.instagram_manager.get_service(channel_id)

    def _fetch_youtube_video_ids(self, yt, youtube_channel_id: str) -> list[str]:
        uploads_playlist_id = "UU" + youtube_channel_id[2:]
        video_ids: list[str] = []
        next_page = None
        while True:
            request = yt._youtube.playlistItems().list(
                part="contentDetails",
                playlistId=uploads_playlist_id,
                maxResults=50,
                pageToken=next_page,
            )
            response = request.execute()
            for item in response.get("items", []):
                video_ids.append(item["contentDetails"]["videoId"])
            next_page = response.get("nextPageToken")
            if not next_page:
                break
        return video_ids

    def _check_youtube_live_status(self, yt, youtube_video_ids: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        now = now_ist()
        for i in range(0, len(youtube_video_ids), 50):
            batch = youtube_video_ids[i : i + 50]
            resp = yt._youtube.videos().list(part="status,snippet", id=",".join(batch)).execute()
            for item in resp.get("items", []):
                vid_id = item["id"]
                privacy = item.get("status", {}).get("privacyStatus", "")
                published_at_str = item.get("snippet", {}).get("publishedAt")
                is_live = privacy == "public"
                published_at_dt = None
                if published_at_str:
                    try:
                        published_at_dt = isoparse(published_at_str).astimezone(IST)
                        if published_at_dt > now:
                            is_live = False
                    except Exception:
                        pass
                result[vid_id] = {"live": is_live, "published_at": published_at_dt}
        return result

    async def get_sync_status(self, channel_id: str) -> dict:
        channel = await self.db.channels.find_one({"channel_id": channel_id})
        if not channel:
            return {"available": False, "reason": "Channel not found"}
        platform = get_channel_platform(channel)
        if platform == "instagram":
            return await self._get_instagram_sync_status(channel_id, channel)
        if not channel.get("youtube_channel_id"):
            return {"available": False, "reason": "No YouTube channel linked"}
        yt = await self._get_youtube_service(channel_id)
        if not yt:
            return {"available": False, "reason": "No YouTube token"}
        try:
            yt_video_ids = set(self._fetch_youtube_video_ids(yt, channel["youtube_channel_id"]))
        except Exception:
            return {"available": False, "reason": "Failed to reach YouTube API"}
        db_yt_ids = {
            doc["youtube_video_id"]
            async for doc in self.db.videos.find(
                {"channel_id": channel_id, "youtube_video_id": {"$ne": None}},
                {"youtube_video_id": 1},
            )
        }
        new_vids = yt_video_ids - db_yt_ids
        metadata_ref = yt_video_ids & db_yt_ids
        scheduled_docs = await self.db.videos.find(
            {"channel_id": channel_id, "status": "scheduled", "youtube_video_id": {"$ne": None}},
            {"youtube_video_id": 1},
        ).to_list(length=None)
        pending_reconciliation = 0
        if scheduled_docs:
            try:
                live_status = self._check_youtube_live_status(yt, [d["youtube_video_id"] for d in scheduled_docs])
                pending_reconciliation = sum(1 for info in live_status.values() if info["live"])
            except Exception:
                pass
        return {
            "available": True,
            "youtube_total": len(yt_video_ids),
            "in_database": len(db_yt_ids),
            "new_videos_to_import": len(new_vids),
            "pending_reconciliation": pending_reconciliation,
            "metadata_to_refresh": len(metadata_ref),
        }

    async def _get_instagram_sync_status(self, channel_id: str, channel: dict) -> dict:
        ig_svc = await self._get_instagram_service(channel_id)
        if not ig_svc:
            return {"available": False, "reason": "No Instagram token"}
        ig_user_id = channel.get("instagram_user_id")
        if not ig_user_id:
            return {"available": False, "reason": "No instagram_user_id"}
        try:
            reels = ig_svc.get_reels(ig_user_id)
            ig_media_ids = {r["id"] for r in reels}
        except Exception:
            return {"available": False, "reason": "Failed to reach Instagram API"}
        db_ig_ids = {
            doc["instagram_media_id"]
            async for doc in self.db.videos.find(
                {"channel_id": channel_id, "instagram_media_id": {"$ne": None}},
                {"instagram_media_id": 1},
            )
        }
        return {
            "available": True,
            "instagram_total": len(ig_media_ids),
            "in_database": len(db_ig_ids),
            "new_reels_to_import": len(ig_media_ids - db_ig_ids),
            "metadata_to_refresh": len(ig_media_ids & db_ig_ids),
        }

    # --- Core Business Logic ---

    async def list_videos(
        self,
        channel_id: str,
        status_filter: str | None = None,
        verification_status: str | None = None,
        suggest_n: int | None = None,
    ) -> dict[str, Any]:
        query: dict = {"channel_id": channel_id}
        if status_filter and status_filter != "all":
            query["status"] = status_filter
        if verification_status:
            if verification_status == "missing":
                query["verification_status"] = None
            else:
                query["verification_status"] = verification_status
        if suggest_n and suggest_n > 0:
            await self.db.videos.update_many(
                {"channel_id": channel_id, "suggested": True},
                {"$set": {"suggested": False, "updated_at": now_ist()}},
            )
            categories = (
                await self.db.categories.find({"channel_id": channel_id, "status": "active"})
                .sort("score", -1)
                .to_list(length=None)
            )
            cat_order = {c["name"]: idx for idx, c in enumerate(categories)}
            todo_videos = await self.db.videos.find({"channel_id": channel_id, "status": "todo"}).to_list(length=None)
            todo_videos.sort(key=lambda v: cat_order.get(v.get("category", ""), 9999))
            for v in todo_videos[:suggest_n]:
                await self.db.videos.update_one(
                    {"_id": v["_id"]}, {"$set": {"suggested": True, "updated_at": now_ist()}}
                )
        projection = {"retention": 0, "performance": 0}
        videos = await self.db.videos.find(query, projection).to_list(length=None)
        for v in videos:
            v.pop("_id", None)
            for key in ("scheduled_at", "published_at", "created_at", "updated_at"):
                if v.get(key) is not None:
                    v[key] = to_ist_iso(v[key])
        sync_status = await self.get_sync_status(channel_id)
        return {"videos": videos, "sync_status": sync_status}

    async def update_video_status(self, channel_id: str, video_id: str, new_status: str) -> dict[str, Any]:
        transitions = {
            "todo": {"published", "ready", "processing"},
            "processing": {"ready", "queued", "todo"},
            "ready": {"todo", "published", "queued"},
            "queued": {"todo", "ready"},
            "scheduled": {"todo", "published"},
            "published": set(),
        }

        video = await self.db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
        if not video:
            raise ValueError(f"Video {video_id} not found")

        old_status = video.get("status")
        if new_status not in transitions.get(old_status, set()):
            raise ValueError(f"Invalid transition {old_status}->{new_status}")

        update_fields = {"status": new_status, "updated_at": now_ist()}
        if old_status == "ready":
            if video.get("r2_object_key"):
                await self._safe_delete_r2(video["r2_object_key"])
            await self.db.posting_queue.delete_one({"channel_id": channel_id, "video_id": video_id})
            update_fields["r2_object_key"] = None
        if old_status in ("queued", "scheduled"):
            await self.db.schedule_queue.delete_one({"channel_id": channel_id, "video_id": video_id})
            update_fields["scheduled_at"] = None
            if old_status == "queued" and new_status == "ready":
                last = await self.db.posting_queue.find_one({"channel_id": channel_id}, sort=[("position", -1)])
                await self.db.posting_queue.insert_one(
                    {
                        "channel_id": channel_id,
                        "video_id": video_id,
                        "position": (last["position"] + 1) if last else 1,
                        "added_at": now_ist(),
                    }
                )
        if new_status == "published":
            update_fields["published_at"] = now_ist()
        await self.db.videos.update_one({"_id": video["_id"]}, {"$set": update_fields})
        if video.get("category") and (new_status == "published" or old_status == "published"):
            await recompute_category(channel_id, video["category"], self.db)
        return {"ok": True, "video_id": video_id, "status": new_status}

    async def change_video_category(
        self, channel_id: str, video_id: str, old_cat_id: str, new_cat_id: str
    ) -> dict[str, Any]:

        video = await self.db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
        if not video:
            raise ValueError("Video not found")
        old_cat = await self.db.categories.find_one({"id": old_cat_id, "channel_id": channel_id})
        new_cat = await self.db.categories.find_one({"id": new_cat_id, "channel_id": channel_id})
        if not old_cat or not new_cat:
            raise ValueError("Category not found")
        old_name, new_name = old_cat["name"], new_cat["name"]
        await self.db.videos.update_one(
            {"channel_id": channel_id, "video_id": video_id},
            {"$set": {"category": new_name, "updated_at": now_ist()}},
        )
        await self.db.analysis_history.update_many(
            {"channel_id": channel_id, "video_id": video_id}, {"$set": {"category": new_name}}
        )
        await recompute_category(channel_id, old_name, self.db)
        await recompute_category(channel_id, new_name, self.db)
        return {
            "ok": True,
            "video_id": video_id,
            "old_category": old_name,
            "new_category": new_name,
        }

    async def delete_video(self, channel_id: str, video_id: str) -> dict[str, Any]:
        video = await self.db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
        if not video:
            raise ValueError("Video not found")
        if video.get("r2_object_key"):
            await self._safe_delete_r2(video["r2_object_key"])
        await self.db.posting_queue.delete_one({"channel_id": channel_id, "video_id": video_id})
        await self.db.schedule_queue.delete_one({"channel_id": channel_id, "video_id": video_id})
        await self.db.videos.delete_one({"_id": video["_id"]})
        return {"ok": True, "video_id": video_id, "deleted": True}

    async def upload_video(self, channel_id: str, video_id: str, file: Any) -> dict[str, Any]:
        video = await self.db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
        if not video:
            raise ValueError("Video not found")
        if video.get("status") != "todo":
            raise ValueError("Video must be in 'todo' status")
        key = f"{channel_id}/{video_id}.mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            shutil.copyfileobj(file.file, tmp)
            tpath = tmp.name
        with open(tpath, "rb") as f:
            assert self.r2 is not None
            self.r2.upload_video(f, key)
        now = now_ist()
        await self.db.videos.update_one(
            {"_id": video["_id"]},
            {"$set": {"status": "ready", "r2_object_key": key, "updated_at": now}},
        )
        last = await self.db.posting_queue.find_one({"channel_id": channel_id}, sort=[("position", -1)])
        await self.db.posting_queue.insert_one(
            {
                "channel_id": channel_id,
                "video_id": video_id,
                "position": (last["position"] + 1) if last else 1,
                "added_at": now,
            }
        )
        self.trigger_retention_analysis(channel_id, video_id, local_video_path=tpath)
        return {"ok": True}

    async def repost_video(self, channel_id: str, video_id: str, data: dict[str, Any]) -> dict[str, Any]:
        video = await self.db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
        if not video:
            raise ValueError("Video not found")
        channel = await self.db.channels.find_one({"channel_id": channel_id})
        platform = get_channel_platform(channel or {})
        new_id = str(uuid.uuid4())
        tid = data.get("target_channel_id") or channel_id

        # Reposts should not be 'todo' (which represents ideas).
        # They start as 'ready' (or 'queued' if we know they are going to be scheduled).
        now = now_ist()
        sch_at = data.get("scheduled_at")
        # We start as 'processing' regardless of schedule.
        # The background download task will promote it to 'ready' or 'queued' once the file is in R2.
        initial_status = "processing"

        # Increment repost_count on the original and capture repost_index for the new video
        await self.db.videos.update_one(
            {"video_id": video_id},
            {"$inc": {"repost_count": 1}, "$set": {"updated_at": now}},
        )
        updated_original = await self.db.videos.find_one({"video_id": video_id})
        repost_index = (updated_original or {}).get("repost_count", 1)

        if platform == "instagram":
            src_ig = video.get("instagram_media_id")
            src_r2 = video.get("r2_object_key")
            if not src_ig and not src_r2:
                raise ValueError(
                    "Cannot repost this reel: it has no Instagram media id and no stored video file. "
                    "Sync the channel or ensure the video was uploaded through this app."
                )
            doc = {
                "channel_id": tid,
                "video_id": new_id,
                "title": data["title"],
                "description": data.get("description", ""),
                "tags": data.get("tags", []),
                "category": video.get("category") or "Uncategorized",
                "status": initial_status,
                "youtube_video_id": None,
                "instagram_media_id": src_ig,
                "thumbnail_url": video.get("thumbnail_url"),
                "scheduled_at": sch_at,
                "is_repost": True,
                "original_video_id": video_id,
                "repost_index": repost_index,
                "repost_count": 0,
                "created_at": now,
                "updated_at": now,
            }
            await self.db.videos.insert_one(doc)
            if data.get("instant"):
                assert self.r2 is not None
                if src_r2:
                    key = src_r2  # reuse same R2 object — no copy needed
                else:
                    ig = await self._get_instagram_service(channel_id)
                    if not ig:
                        raise ValueError("No Instagram token")
                    assert src_ig is not None
                    media_url = ig.get_reel_media_url(src_ig)
                    key = await download_instagram_media_to_r2(tid, media_url, self.r2)
                
                await self.db.videos.update_one(
                    {"video_id": new_id},
                    {
                        "$set": {
                            "r2_object_key": key,
                            "status": "queued" if (sch_at and sch_at > now_ist()) else "ready",
                            "updated_at": now_ist(),
                        }
                    },
                )
                
                # Add to appropriate queue since it's instant
                if sch_at and sch_at > now_ist():
                    last = await self.db.schedule_queue.find_one({"channel_id": tid}, sort=[("position", -1)])
                    await self.db.schedule_queue.insert_one({
                        "channel_id": tid,
                        "video_id": new_id,
                        "position": (last["position"] + 1) if last else 1,
                        "scheduled_at": sch_at,
                        "added_at": now,
                    })
                else:
                    last = await self.db.posting_queue.find_one({"channel_id": tid}, sort=[("position", -1)])
                    await self.db.posting_queue.insert_one({
                        "channel_id": tid,
                        "video_id": new_id,
                        "position": (last["position"] + 1) if last else 1,
                        "added_at": now,
                    })
            else:
                self.trigger_repost_download(
                    channel_id,
                    new_id,
                    target_channel_id=tid,
                    instagram_media_id=None if src_r2 else src_ig,
                    source_r2_key=src_r2 if src_r2 else None,
                )
            return {"ok": True, "new_video_id": new_id}

        if not video.get("youtube_video_id"):
            raise ValueError("Original YouTube video not found")
            
        doc = {
            "channel_id": tid,
            "video_id": new_id,
            "title": data["title"],
            "description": data.get("description", ""),
            "tags": data.get("tags", []),
            "category": video.get("category") or "Uncategorized",
            "status": initial_status,
            "youtube_video_id": video["youtube_video_id"],
            "thumbnail_url": video.get("thumbnail_url"),
            "scheduled_at": sch_at,
            "is_repost": True,
            "original_video_id": video_id,
            "repost_index": repost_index,
            "repost_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        await self.db.videos.insert_one(doc)
        if data.get("instant"):
            assert self.r2 is not None
            src_r2 = video.get("r2_object_key")
            if src_r2:
                key = src_r2  # reuse same R2 object — no copy needed
            else:
                key = await download_youtube_video_to_r2(video["youtube_video_id"], tid, self.r2)
            
            await self.db.videos.update_one(
                {"video_id": new_id},
                {
                    "$set": {
                        "r2_object_key": key,
                        "status": "queued" if (sch_at and sch_at > now_ist()) else "ready",
                        "updated_at": now_ist(),
                    }
                },
            )
            
            # Add to appropriate queue since it's instant
            if sch_at and sch_at > now_ist():
                last = await self.db.schedule_queue.find_one({"channel_id": tid}, sort=[("position", -1)])
                await self.db.schedule_queue.insert_one({
                    "channel_id": tid,
                    "video_id": new_id,
                    "position": (last["position"] + 1) if last else 1,
                    "scheduled_at": sch_at,
                    "added_at": now,
                })
            else:
                last = await self.db.posting_queue.find_one({"channel_id": tid}, sort=[("position", -1)])
                await self.db.posting_queue.insert_one({
                    "channel_id": tid,
                    "video_id": new_id,
                    "position": (last["position"] + 1) if last else 1,
                    "added_at": now,
                })
        else:
            src_r2 = video.get("r2_object_key")
            self.trigger_repost_download(
                channel_id,
                new_id,
                target_channel_id=tid,
                youtube_video_id=None if src_r2 else video["youtube_video_id"],
                source_r2_key=src_r2 if src_r2 else None,
            )
        return {"ok": True, "new_video_id": new_id}

    async def mark_repost_status(
        self,
        channel_id: str,
        video_id: str,
        is_repost: bool,
        original_video_id: str | None,
    ) -> dict:
        video = await self.db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
        if not video:
            raise ValueError("Video not found")

        now = now_ist()

        if is_repost:
            if not original_video_id:
                raise ValueError("original_video_id is required when marking as repost")
            original = await self.db.videos.find_one({"video_id": original_video_id})
            if not original:
                raise ValueError("Original video not found")

            # Un-link from any previous original first
            prev_original_id = video.get("original_video_id")
            if prev_original_id and prev_original_id != original_video_id:
                await self.db.videos.update_one(
                    {"video_id": prev_original_id},
                    {"$inc": {"repost_count": -1}, "$set": {"updated_at": now}},
                )

            # Increment repost_count on new original
            if not video.get("is_repost") or prev_original_id != original_video_id:
                await self.db.videos.update_one(
                    {"video_id": original_video_id},
                    {"$inc": {"repost_count": 1}, "$set": {"updated_at": now}},
                )
            updated_original = await self.db.videos.find_one({"video_id": original_video_id})
            repost_index = (updated_original or {}).get("repost_count", 1)

            await self.db.videos.update_one(
                {"video_id": video_id},
                {
                    "$set": {
                        "is_repost": True,
                        "original_video_id": original_video_id,
                        "repost_index": repost_index,
                        "updated_at": now,
                    }
                },
            )
        else:
            # Un-mark as repost — decrement original's repost_count
            prev_original_id = video.get("original_video_id")
            if prev_original_id:
                await self.db.videos.update_one(
                    {"video_id": prev_original_id},
                    {"$inc": {"repost_count": -1}, "$set": {"updated_at": now}},
                )
            await self.db.videos.update_one(
                {"video_id": video_id},
                {
                    "$set": {
                        "is_repost": False,
                        "original_video_id": None,
                        "repost_index": None,
                        "updated_at": now,
                    }
                },
            )

        return {"ok": True}

    def trigger_retention_analysis(self, channel_id: str, video_id: str, local_video_path: str | None = None) -> None:

        if self.r2 and self.gemini:
            create_monitored_task(
                run_retention_analysis(
                    channel_id,
                    video_id,
                    self.db,
                    self.r2,
                    self.gemini,
                    local_video_path=local_video_path,
                ),
                feature="Retention analysis (scheduled from VideoService)",
                context={"channel_id": channel_id, "video_id": video_id},
            )

    def trigger_repost_download(
        self,
        channel_id: str,
        new_video_id: str,
        target_channel_id: str | None = None,
        *,
        youtube_video_id: str | None = None,
        instagram_media_id: str | None = None,
        source_r2_key: str | None = None,
    ) -> None:

        if self.r2:

            async def _job():
                try:
                    tid = target_channel_id or channel_id
                    assert self.r2 is not None
                    if youtube_video_id:
                        key = await download_youtube_video_to_r2(youtube_video_id, tid, self.r2)
                    elif source_r2_key:
                        key = source_r2_key  # reuse same R2 object — no copy needed
                    elif instagram_media_id:
                        ig = await self._get_instagram_service(channel_id)
                        if not ig:
                            raise RuntimeError("No Instagram token")
                        media_url = ig.get_reel_media_url(instagram_media_id)
                        key = await download_instagram_media_to_r2(tid, media_url, self.r2)
                    else:
                        raise RuntimeError(
                            "Repost download requires youtube_video_id, instagram_media_id, or source_r2_key"
                        )
                    video = await self.db.videos.find_one({"channel_id": tid, "video_id": new_video_id})
                    if not video:
                        return
                    now = now_ist()
                    # Determine final status based on whether it's scheduled
                    sch_at = video.get("scheduled_at")
                    if sch_at and sch_at.tzinfo is None:
                        from app.timezone import UTC
                        sch_at = sch_at.replace(tzinfo=UTC)
                    final_status = "queued" if (sch_at and sch_at > now) else "ready"
                    
                    upd = {"r2_object_key": key, "updated_at": now, "status": final_status}
                    
                    if final_status == "queued":
                        await self.db.videos.update_one({"_id": video["_id"]}, {"$set": upd})
                        last = await self.db.schedule_queue.find_one({"channel_id": tid}, sort=[("position", -1)])
                        await self.db.schedule_queue.insert_one(
                            {
                                "channel_id": tid,
                                "video_id": new_video_id,
                                "position": (last["position"] + 1) if last else 1,
                                "scheduled_at": sch_at,
                                "added_at": now,
                            }
                        )
                    else:
                        await self.db.videos.update_one({"_id": video["_id"]}, {"$set": upd})
                        last = await self.db.posting_queue.find_one({"channel_id": tid}, sort=[("position", -1)])
                        await self.db.posting_queue.insert_one(
                            {
                                "channel_id": tid,
                                "video_id": new_video_id,
                                "position": (last["position"] + 1) if last else 1,
                                "added_at": now,
                            }
                        )

                except Exception as e:
                    tid = target_channel_id or channel_id
                    # Reset status on failure so it's not stuck in 'processing'
                    try:
                        await self.db.videos.update_one(
                            {"channel_id": tid, "video_id": new_video_id},
                            {"$set": {"status": "ready", "updated_at": now_ist()}},
                        )
                    except Exception:
                        pass

                    ctx: dict[str, Any] = {
                        "channel_id": channel_id,
                        "new_video_id": new_video_id,
                        "target_channel_id": tid,
                    }
                    if youtube_video_id:
                        ctx["youtube_video_id"] = youtube_video_id
                    if instagram_media_id:
                        ctx["instagram_media_id"] = instagram_media_id
                    if source_r2_key:
                        ctx["source_r2_key"] = source_r2_key
                    await report_error(
                        feature="Repost download → R2",
                        message=f"Repost download failed: {e!s}",
                        exception=e,
                        context=ctx,
                    )

            create_monitored_task(_job(), feature="Repost download job", context={"new_video_id": new_video_id})

    async def extract_content_params(self, channel_id: str, video_id: str) -> dict[str, Any]:
        if not self.gemini:
            raise ValueError("Gemini not initialised")
        video = await self.db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
        if not video:
            raise ValueError("Video not found")
        schema = await get_content_schema_for_prompt(self.db, channel_id)
        if not schema:
            # mypy: disable-error-code: attr-defined
            raise ValueError("No schema defined")
        prompt = (
            f"Extract params:\nSchema: {json.dumps(schema)}\n"
            f"Video: {video.get('title')}\n{video.get('description')[:1000]}"
        )

        try:
            params = json.loads(await self.gemini._generate(prompt))
            await self.db.videos.update_one(
                {"_id": video["_id"]},
                {
                    "$set": {
                        "content_params": params,
                        "verification_status": "unverified",
                        "updated_at": now_ist(),
                    }
                },
            )
            return {
                "ok": True,
                "video_id": video_id,
                "content_params": params,
                "verification_status": "unverified",
            }
        except Exception:
            raise ValueError("Gemini extraction failed")

    async def create_video(
        self,
        channel_id: str,
        file: Any,
        title: str,
        description: str = "",
        tags: str = "",
        category: str | None = None,
        content_params: str | None = None,
        scheduled_at: str | None = None,
    ) -> dict[str, Any]:
        channel = await self.db.channels.find_one({"channel_id": channel_id})
        if not channel:
            raise ValueError("Channel not found")
        params = json.loads(content_params) if content_params else None
        platform = get_channel_platform(channel)
        parsed_tags = [t.strip() for t in tags.split(",")] if tags else []
        sch_at = isoparse(scheduled_at).replace(tzinfo=IST) if scheduled_at else None
        vid_id = str(uuid.uuid4())
        key = f"{channel_id}/{vid_id}.mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            shutil.copyfileobj(file, tmp)
            tpath = tmp.name
        with open(tpath, "rb") as f:
            assert self.r2 is not None
            self.r2.upload_video(f, key)
        now = now_ist()
        status = "scheduled" if (sch_at and platform == "instagram") else "ready"
        doc = {
            "channel_id": channel_id,
            "video_id": vid_id,
            "title": title,
            "description": description,
            "tags": parsed_tags,
            "category": category or "Uncategorized",
            "status": status,
            "r2_object_key": key,
            "content_params": params,
            "verification_status": "verified" if (category and params) else "unverified",
            "scheduled_at": sch_at if status == "scheduled" else None,
            "created_at": now,
            "updated_at": now,
        }
        await self.db.videos.insert_one(doc)
        if status == "scheduled":
            last = await self.db.schedule_queue.find_one({"channel_id": channel_id}, sort=[("position", -1)])
            await self.db.schedule_queue.insert_one(
                {
                    "channel_id": channel_id,
                    "video_id": vid_id,
                    "position": (last["position"] + 1) if last else 1,
                    "scheduled_at": sch_at,
                    "added_at": now,
                }
            )
        else:
            last = await self.db.posting_queue.find_one({"channel_id": channel_id}, sort=[("position", -1)])
            await self.db.posting_queue.insert_one(
                {
                    "channel_id": channel_id,
                    "video_id": vid_id,
                    "position": (last["position"] + 1) if last else 1,
                    "added_at": now,
                }
            )
        if status in ("ready", "scheduled"):
            self.trigger_retention_analysis(channel_id, vid_id, local_video_path=tpath)
        elif os.path.exists(tpath):
            os.unlink(tpath)
        doc.pop("_id", None)
        return {"ok": True, "video": doc}

    async def create_multi_channel_video(
        self,
        primary_channel_id: str,
        file: Any,
        channels: list[dict],
    ) -> dict[str, Any]:
        """Upload a video file once and create records for every target channel.

        All records share the same R2 object key.  AI packaging (retention
        analysis) runs once on the primary channel's record; after it
        completes the service propagates platform-appropriate packaging to
        every sibling record.

        Parameters
        ----------
        primary_channel_id:
            The channel the file is uploaded under.  Also used as the R2
            prefix for the object key.
        file:
            File-like object (from the multipart upload).
        channels:
            List of per-channel configs::

                [
                  {
                    "channel_id": "...",
                    "title": "...",
                    "description": "...",
                    "tags": [...],          # list[str]
                    "category": "...",
                    "content_params": {...},
                    "scheduled_at": "...",  # ISO string or None
                  },
                  ...
                ]

        Returns
        -------
        {"ok": True, "group_id": ..., "primary_video_id": ...,
         "channel_videos": [{"channel_id": ..., "video_id": ...}, ...]}
        """
        if not channels:
            raise ValueError("At least one channel config is required")
        assert self.r2 is not None

        group_id = str(uuid.uuid4())
        primary_vid_id = str(uuid.uuid4())
        r2_key = f"{primary_channel_id}/{primary_vid_id}.mp4"

        # ── 1. Upload file to R2 once ────────────────────────────────────
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            shutil.copyfileobj(file, tmp)
            tpath = tmp.name
        with open(tpath, "rb") as f:
            self.r2.upload_video(f, r2_key)

        now = now_ist()
        channel_videos: list[dict] = []
        primary_doc: dict | None = None

        # ── 2. Create a video record for every target channel ────────────
        for ch_cfg in channels:
            cid = ch_cfg["channel_id"]
            channel = await self.db.channels.find_one({"channel_id": cid})
            if not channel:
                continue

            is_primary = cid == primary_channel_id
            vid_id = primary_vid_id if is_primary else str(uuid.uuid4())
            platform = get_channel_platform(channel)

            raw_tags = ch_cfg.get("tags") or []
            parsed_tags = (
                [t.strip() for t in raw_tags.split(",") if t.strip()]
                if isinstance(raw_tags, str)
                else list(raw_tags)
            )

            sch_at: datetime | None = None
            if ch_cfg.get("scheduled_at"):
                try:
                    sch_at = isoparse(ch_cfg["scheduled_at"]).replace(tzinfo=IST)
                except Exception:
                    pass

            status = "scheduled" if (sch_at and platform == "instagram") else "ready"

            doc = {
                "channel_id": cid,
                "video_id": vid_id,
                "title": ch_cfg.get("title", ""),
                "description": ch_cfg.get("description", ""),
                "tags": parsed_tags,
                "category": ch_cfg.get("category") or "Uncategorized",
                "status": status,
                "r2_object_key": r2_key,  # shared across all channel records
                "content_params": ch_cfg.get("content_params"),
                "verification_status": (
                    "verified"
                    if (ch_cfg.get("category") and ch_cfg.get("content_params"))
                    else "unverified"
                ),
                "scheduled_at": sch_at if status == "scheduled" else None,
                "multi_channel_group_id": group_id,
                "created_at": now,
                "updated_at": now,
            }
            await self.db.videos.insert_one(doc)
            channel_videos.append({"channel_id": cid, "video_id": vid_id})

            if is_primary:
                primary_doc = dict(doc)

            # Add to queues
            if status == "scheduled":
                last = await self.db.schedule_queue.find_one({"channel_id": cid}, sort=[("position", -1)])
                await self.db.schedule_queue.insert_one({
                    "channel_id": cid, "video_id": vid_id,
                    "position": (last["position"] + 1) if last else 1,
                    "scheduled_at": sch_at, "added_at": now,
                })
            else:
                last = await self.db.posting_queue.find_one({"channel_id": cid}, sort=[("position", -1)])
                await self.db.posting_queue.insert_one({
                    "channel_id": cid, "video_id": vid_id,
                    "position": (last["position"] + 1) if last else 1,
                    "added_at": now,
                })

        if not primary_doc:
            raise ValueError("Primary channel config missing from channels list")

        # ── 3. Trigger retention analysis once on the primary record ────
        #    retention_analysis.py will propagate packaging to siblings via
        #    multi_channel_group_id after the Gemini call completes.
        self.trigger_retention_analysis(primary_channel_id, primary_vid_id, local_video_path=tpath)

        return {
            "ok": True,
            "group_id": group_id,
            "primary_video_id": primary_vid_id,
            "channel_videos": channel_videos,
        }

    async def schedule_video(
        self, channel_id: str, video_id: str, scheduled_at: datetime | None = None
    ) -> dict[str, Any]:

        settings = get_settings()
        channel = await self.db.channels.find_one({"channel_id": channel_id})
        if not channel:
            raise ValueError("Channel not found")
        platform = channel.get("platform", "youtube")
        if video_id.lower() == "all":
            entries = (
                await self.db.posting_queue.find({"channel_id": channel_id}).sort("position", 1).to_list(length=None)
            )
            vids = []
            for e in entries:
                v = await self.db.videos.find_one({"channel_id": channel_id, "video_id": e["video_id"]})
                if v and v.get("status") == "ready":
                    vids.append(v)
        else:
            v = await self.db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
            if not v or v.get("status") != "ready":
                raise ValueError("Video not ready")
            vids = [v]
        if not vids:
            raise ValueError("No videos to schedule")
        if scheduled_at and video_id.lower() != "all":
            slots = [scheduled_at]
        else:
            analysis = await self.db.analysis.find_one({"channel_id": channel_id})
            if not analysis:
                raise ValueError("No analysis found")
            existing = await self.db.schedule_queue.find({"channel_id": channel_id}).to_list(length=None)
            slots = compute_schedule_slots(
                analysis["best_posting_times"],
                [e.get("scheduled_at") for e in existing],
                len(vids),
                settings.TIMEZONE,
            )
        if len(slots) < len(vids):
            raise ValueError("Not enough slots")
        res = []
        for v_doc, slot in zip(vids, slots, strict=False):
            if platform == "youtube":
                r = await enqueue_video_for_youtube(
                    db=self.db, channel_id=channel_id, video_doc=v_doc, scheduled_at=slot
                )
            else:
                r = await schedule_single_video_instagram(
                    db=self.db, channel_id=channel_id, video_doc=v_doc, scheduled_at=slot
                )
            res.append(r)
        return {"ok": True, "videos": res}

    async def reschedule_video(
        self, channel_id: str, video_id: str, new_time: datetime | None = None
    ) -> dict[str, Any]:
        video = await self.db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
        if not video:
            raise ValueError("Video not found")
        if video.get("status") != "queued":
            raise ValueError("Video not queued")
        now = now_ist()
        await self.db.videos.update_one({"_id": video["_id"]}, {"$set": {"scheduled_at": new_time, "updated_at": now}})
        await self.db.schedule_queue.update_one(
            {"channel_id": channel_id, "video_id": video_id}, {"$set": {"scheduled_at": new_time}}
        )
        return {"ok": True, "scheduled_at": to_ist_iso(new_time) if new_time else None}

    async def update_video_metadata(self, channel_id: str, video_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        video = await self.db.videos.find_one({"channel_id": channel_id, "video_id": video_id})
        if not video:
            raise ValueError("Video not found")
        upd: dict = {"updated_at": now_ist()}
        for key in ("title", "description", "category", "thumbnail_url"):
            if metadata.get(key) is not None:
                upd[key] = metadata[key]
        if metadata.get("tags") is not None:
            if metadata.get("tag_mode") == "append":
                upd["tags"] = list(set((video.get("tags") or []) + metadata["tags"]))
            else:
                upd["tags"] = metadata["tags"]
        await self.db.videos.update_one({"_id": video["_id"]}, {"$set": upd})
        return {"ok": True}

    async def sync_videos(self, channel_id: str, instructions: str | None = None) -> dict[str, Any]:
        channel = await self.db.channels.find_one({"channel_id": channel_id})
        if not channel:
            raise ValueError("Channel not found")
        platform = get_channel_platform(channel)
        if platform == "youtube":
            result = await self._sync_youtube_videos(channel_id, channel, instructions)
        elif platform == "instagram":
            result = await self._sync_instagram_videos(channel_id, channel, instructions)
        else:
            return {"ok": True, "message": "Platform not supported"}
        await update_channel_task_status(self.db, channel_id, "video_sync")
        return result

    async def _sync_youtube_videos(self, channel_id: str, channel: dict, instructions: str | None) -> dict[str, Any]:
        yt = await self._get_youtube_service(channel_id)
        if not yt:
            raise ValueError("No YouTube token")
        yt_vids = self._fetch_all_youtube_videos(yt, channel["youtube_channel_id"])
        db_ids = {
            doc["youtube_video_id"]
            async for doc in self.db.videos.find(
                {"channel_id": channel_id, "youtube_video_id": {"$ne": None}},
                {"youtube_video_id": 1},
            )
        }
        new_vids = [v for v in yt_vids if v["youtube_video_id"] not in db_ids]
        for v in [v for v in yt_vids if v["youtube_video_id"] in db_ids]:
            existing = await self.db.videos.find_one(
                {"channel_id": channel_id, "youtube_video_id": v["youtube_video_id"]}
            )
            # Determine status based on privacy
            target_status = "published"
            if v.get("youtube_privacy_status") == "private":
                target_status = "scheduled"

            upd: dict[str, Any] = {
                "title": v["title"],
                "description": v["description"],
                "metadata.views": v["views"],
                "metadata.youtube_privacy_status": v.get("youtube_privacy_status"),
                "status": target_status,
                "updated_at": now_ist(),
            }

            # Update timing
            if target_status == "published":
                upd["published_at"] = v["published_at"] or now_ist()
            elif v.get("scheduled_at"):
                upd["scheduled_at"] = v["scheduled_at"]

            if not self._has_thumbnail_url(existing) and v.get("thumbnail_url"):
                upd["thumbnail_url"] = v["thumbnail_url"]

            await self.db.videos.update_one(
                {"channel_id": channel_id, "youtube_video_id": v["youtube_video_id"]},
                {"$set": upd},
            )

        if not new_vids:
            return {"ok": True, "synced": 0}
        schema = await get_content_schema_for_prompt(self.db, channel_id, include_belongs_to=True)
        cats = [
            {"name": c["name"], "description": c.get("description", "")}
            async for c in self.db.categories.find({"channel_id": channel_id})
        ]
        inserted = 0
        for i in range(0, len(new_vids), 10):
            batch = new_vids[i : i + 10]
            results = await self._extract_params_and_categorize_batch(schema, cats, batch, instructions, "youtube")
            res_map = {r["youtube_video_id"]: r for r in results if "youtube_video_id" in r}
            for v in batch:
                ana = res_map.get(v["youtube_video_id"], {})
                cat = ana.get("category", "Uncategorized")
                if cat not in [c["name"] for c in cats]:
                    await self.db.categories.insert_one(
                        {
                            "id": str(uuid.uuid4()),
                            "channel_id": channel_id,
                            "name": cat,
                            "status": "active",
                            "created_at": now_ist(),
                        }
                    )
                    cats.append({"name": cat})
                target_status = "published"
                if v.get("youtube_privacy_status") == "private":
                    target_status = "scheduled"

                await self.db.videos.insert_one(
                    {
                        "channel_id": channel_id,
                        "video_id": str(uuid.uuid4()),
                        "youtube_video_id": v["youtube_video_id"],
                        "title": v["title"],
                        "description": v["description"],
                        "category": cat,
                        "status": target_status,
                        "published_at": v["published_at"] if target_status == "published" else None,
                        "scheduled_at": v.get("scheduled_at") if target_status == "scheduled" else None,
                        "thumbnail_url": v.get("thumbnail_url"),
                        "metadata": {
                            "youtube_privacy_status": v.get("youtube_privacy_status"),
                        },
                        "content_params": ana.get("content_params"),
                        "verification_status": "unverified",
                        "created_at": now_ist(),
                    }
                )
                inserted += 1
        return {"ok": True, "synced": inserted}

    async def _sync_instagram_videos(self, channel_id: str, channel: dict, instructions: str | None) -> dict[str, Any]:
        ig = await self._get_instagram_service(channel_id)
        if not ig:
            raise ValueError("No Instagram token")
        ig_reels = ig.get_reels(channel["instagram_user_id"])
        db_ids = {
            doc["instagram_media_id"]
            async for doc in self.db.videos.find(
                {"channel_id": channel_id, "instagram_media_id": {"$ne": None}},
                {"instagram_media_id": 1},
            )
        }
        # Batch fetch insights for all media IDs
        media_ids = [r["id"] for r in ig_reels]
        insights_map = ig.get_reel_insights(media_ids)

        new_vids = []
        for r in ig_reels:
            mid = r["id"]
            ins = insights_map.get(mid, {})
            
            # Map metrics correctly
            # video_views is available on the media object directly for Reels
            # reach, saved, shares come from insights
            likes = int(r.get("like_count", 0))
            comments = int(r.get("comments_count", 0))
            reach = int(ins.get("reach", 0))
            views = reach # Use reach as views for Reels since video_views is deprecated
            saves = int(ins.get("saved", 0))
            shares = int(ins.get("shares", 0))

            if mid not in db_ids:
                new_vids.append(
                    {
                        "instagram_media_id": mid,
                        "title": r.get("caption", "Untitled Reel"),
                        "description": r.get("caption", ""),
                        "published_at": (isoparse(r.get("timestamp")) if r.get("timestamp") else None),
                        "thumbnail_url": self._instagram_reel_thumbnail_url(r),
                        "metadata": {
                            "views": views,
                            "likes": likes,
                            "comments": comments,
                            "reach": reach,
                            "saves": saves,
                            "shares": shares,
                        }
                    }
                )
            else:
                mid = r["id"]
                ins = insights_map.get(mid, {})
                likes = int(r.get("like_count", 0))
                comments = int(r.get("comments_count", 0))
                reach = int(ins.get("reach", 0))
                views = reach # Use reach as views for Reels
                saves = int(ins.get("saved", 0))
                shares = int(ins.get("shares", 0))

                existing = await self.db.videos.find_one(
                    {"channel_id": channel_id, "instagram_media_id": mid},
                    {"thumbnail_url": 1},
                )
                thumb = self._instagram_reel_thumbnail_url(r)
                set_doc: dict[str, Any] = {
                    "metadata.views": views,
                    "metadata.likes": likes,
                    "metadata.comments": comments,
                    "metadata.reach": reach,
                    "metadata.saves": saves,
                    "metadata.shares": shares,
                    "status": "published",
                    "updated_at": now_ist(),
                }
                if thumb and not self._has_thumbnail_url(existing):
                    set_doc["thumbnail_url"] = thumb
                await self.db.videos.update_one(
                    {"channel_id": channel_id, "instagram_media_id": mid},
                    {"$set": set_doc},
                )

        if not new_vids:
            return {"ok": True, "synced": 0}
        schema = await get_content_schema_for_prompt(self.db, channel_id, include_belongs_to=True)
        cats = [
            {"name": c["name"], "description": c.get("description", "")}
            async for c in self.db.categories.find({"channel_id": channel_id})
        ]
        inserted = 0
        for i in range(0, len(new_vids), 10):
            batch = new_vids[i : i + 10]
            results = await self._extract_params_and_categorize_batch(schema, cats, batch, instructions, "instagram")
            res_map = {r["instagram_media_id"]: r for r in results if "instagram_media_id" in r}
            for v in batch:
                ana = res_map.get(v["instagram_media_id"], {})
                cat = ana.get("category", "Uncategorized")
                if cat not in [c["name"] for c in cats]:
                    await self.db.categories.insert_one(
                        {
                            "id": str(uuid.uuid4()),
                            "channel_id": channel_id,
                            "name": cat,
                            "status": "active",
                            "created_at": now_ist(),
                        }
                    )
                    cats.append({"name": cat})
                await self.db.videos.insert_one(
                    {
                        "channel_id": channel_id,
                        "video_id": str(uuid.uuid4()),
                        "instagram_media_id": v["instagram_media_id"],
                        "title": v["title"],
                        "description": v["description"],
                        "category": cat,
                        "status": "published",
                        "published_at": v["published_at"],
                        "thumbnail_url": v.get("thumbnail_url"),
                        "metadata": v.get("metadata", {}),
                        "content_params": ana.get("content_params"),
                        "verification_status": "unverified",
                        "created_at": now_ist(),
                    }
                )
                inserted += 1
        return {"ok": True, "synced": inserted}

    async def _extract_params_and_categorize_batch(self, schema, cats, batch, instructions, platform):
        if not self.gemini:
            return []
        id_key = "youtube_video_id" if platform == "youtube" else "instagram_media_id"
        summaries = [{id_key: v[id_key], "title": v["title"], "description": v["description"][:500]} for v in batch]
        prompt = (
            f"Extract params:\nSchema: {json.dumps(schema)}\n"
            f"Categories: {json.dumps(cats)}\nVideos: {json.dumps(summaries)}\n{instructions}"
        )  # noqa: E501
        try:
            res = await self.gemini._generate(prompt)
            return json.loads(res)
        except Exception:
            return []

    def _fetch_all_youtube_videos(self, yt, youtube_channel_id: str):
        uploads_playlist_id = "UU" + youtube_channel_id[2:]
        video_ids = []
        next_page = None
        while True:
            request = yt._youtube.playlistItems().list(
                part="contentDetails",
                playlistId=uploads_playlist_id,
                maxResults=50,
                pageToken=next_page,
            )
            response = request.execute()
            for item in response.get("items", []):
                video_ids.append(item["contentDetails"]["videoId"])
            next_page = response.get("nextPageToken")
            if not next_page:
                break
        videos = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            resp = (
                yt._youtube.videos().list(part="snippet,statistics,contentDetails,status", id=",".join(batch)).execute()
            )
            for item in resp.get("items", []):
                snippet, stats, content = (
                    item.get("snippet", {}),
                    item.get("statistics", {}),
                    item.get("contentDetails", {}),
                )
                st = item.get("status") or {}
                raw_priv = (st.get("privacyStatus") or "").lower()
                youtube_privacy = raw_priv if raw_priv in ("public", "unlisted", "private") else None
                views, likes, comments = (
                    int(stats.get("viewCount", 0)),
                    int(stats.get("likeCount", 0)),
                    int(stats.get("commentCount", 0)),
                )
                videos.append(
                    {
                        "youtube_video_id": item["id"],
                        "title": snippet.get("title", ""),
                        "description": snippet.get("description", ""),
                        "tags": snippet.get("tags", []),
                        "published_at": isoparse(snippet.get("publishedAt")) if snippet.get("publishedAt") else None,
                        "views": views,
                        "likes": likes,
                        "comments": comments,
                        "duration_seconds": self._parse_duration(content.get("duration")),
                        "thumbnail_url": self._youtube_thumbnail_from_snippet(snippet),
                        "youtube_privacy_status": youtube_privacy,
                        "scheduled_at": isoparse(str(st.get("publishAt"))) if st.get("publishAt") else None,
                        **self._compute_rates(views, likes, comments),
                    }
                )
        return videos

    @staticmethod
    def _youtube_thumbnail_from_snippet(snippet: dict[str, Any]) -> str | None:
        """Pick the best available URL from YouTube ``snippet.thumbnails``."""
        thumbs = snippet.get("thumbnails") or {}
        for key in ("maxres", "standard", "high", "medium", "default"):
            url = (thumbs.get(key) or {}).get("url")
            if url:
                return str(url)
        return None

    @staticmethod
    def _has_thumbnail_url(doc: dict[str, Any] | None) -> bool:
        if not doc:
            return False
        u = doc.get("thumbnail_url")
        return isinstance(u, str) and bool(u.strip())

    @staticmethod
    def _instagram_reel_thumbnail_url(reel: dict[str, Any]) -> str | None:
        """Cover image from Graph API (``thumbnail_url`` preferred for video/reel)."""
        for key in ("thumbnail_url", "media_url"):
            val = reel.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None

    def _parse_duration(self, duration: str) -> int:

        match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
        if not match:
            return 0
        return int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60 + int(match.group(3) or 0)

    def _compute_rates(self, views: int, likes: int, comments: int) -> dict:
        if views > 0:
            return {
                "engagement_rate": round((likes + comments) / views * 100, 4),
                "like_rate": round(likes / views * 100, 4),
                "comment_rate": round(comments / views * 100, 4),
            }
        return {"engagement_rate": None, "like_rate": None, "comment_rate": None}
