from __future__ import annotations

"""YouTube Data API v3 + Analytics API service.

Authentication uses OAuth2 tokens stored in the MongoDB ``channels``
collection.  Tokens are refreshed automatically when expired and
written back to the DB.
"""

from datetime import datetime, timezone
from typing import Any

from app.timezone import now_ist

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from app.logger import get_logger

logger = get_logger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


class YouTubeService:
    """Wraps the YouTube Data API and Analytics API.

    Accepts a token dict (from the DB) and client credentials directly.
    When tokens are refreshed, the updated credentials are written back
    to the channel document in MongoDB via ``_save_credentials``.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_data: dict[str, Any],
        *,
        db: Any = None,
        channel_id: str | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._db = db
        self._channel_id = channel_id
        self._creds = self._build_credentials(token_data)
        self._youtube = self._build_data_client()
        self._youtube_analytics = self._build_analytics_client()

    # ------------------------------------------------------------------
    # Client bootstrap
    # ------------------------------------------------------------------

    def _build_credentials(self, token_data: dict[str, Any]) -> Credentials:
        """Construct ``Credentials`` from the DB token dict, refreshing if expired."""
        expiry_raw = token_data.get("expiry")
        expiry_dt = None
        if expiry_raw:
            try:
                if isinstance(expiry_raw, str):
                    expiry_dt = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
                elif isinstance(expiry_raw, datetime):
                    expiry_dt = expiry_raw if expiry_raw.tzinfo else expiry_raw.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        creds = Credentials(
            token=token_data["token"],
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=token_data.get("scopes"),
            expiry=expiry_dt.astimezone(timezone.utc).replace(tzinfo=None) if expiry_dt else None,
        )

        if not creds.valid and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            self._save_credentials(creds)
            logger.info("Refreshed YouTube token for channel '%s'", self._channel_id)

        return creds

    def _build_data_client(self) -> Any:
        """Build an authorised YouTube Data API v3 client."""
        return build("youtube", "v3", credentials=self._creds)

    def _build_analytics_client(self) -> Any | None:
        """Build a YouTube Analytics API v2 client.

        Returns ``None`` if the token lacks the analytics scope.
        """
        try:
            return build("youtubeAnalytics", "v2", credentials=self._creds)
        except Exception as exc:
            logger.warning(
                "Could not build YouTube Analytics client: %s. "
                "Analytics metrics will be unavailable. "
                "Re-authenticate via the frontend to add the analytics scope.",
                exc,
            )
            return None

    def _save_credentials(self, creds: Credentials) -> None:
        """Persist refreshed credentials back to MongoDB (fire-and-forget)."""
        if self._db is None or self._channel_id is None:
            return

        import asyncio

        updated_expiry = (
            creds.expiry.replace(tzinfo=timezone.utc).isoformat()
            if creds.expiry
            else None
        )

        async def _write() -> None:
            await self._db.channels.update_one(
                {"channel_id": self._channel_id},
                {
                    "$set": {
                        "youtube_tokens.token": creds.token,
                        "youtube_tokens.expiry": updated_expiry,
                        "updated_at": now_ist(),
                    }
                },
            )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_write())
        except RuntimeError:
            asyncio.run(_write())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_channel_info(self, youtube_channel_id: str) -> dict[str, Any]:
        """Fetch channel metadata from YouTube.

        Returns a dict with: ``name``, ``description``, ``subscriber_count``,
        ``video_count``, ``thumbnail_url``, ``custom_url``.
        """
        response = (
            self._youtube.channels()
            .list(part="snippet,statistics", id=youtube_channel_id)
            .execute()
        )

        items = response.get("items", [])
        if not items:
            raise ValueError(
                f"No YouTube channel found with ID '{youtube_channel_id}'"
            )

        channel = items[0]
        snippet = channel.get("snippet", {})
        stats = channel.get("statistics", {})

        return {
            "name": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "custom_url": snippet.get("customUrl", ""),
            "thumbnail_url": snippet.get("thumbnails", {})
            .get("default", {})
            .get("url", ""),
            "subscriber_count": int(stats.get("subscriberCount", 0)),
            "video_count": int(stats.get("videoCount", 0)),
            "view_count": int(stats.get("viewCount", 0)),
        }

    @staticmethod
    def _parse_iso8601_duration(duration: str) -> int:
        """Convert ISO 8601 duration (e.g. 'PT1H2M30S') to total seconds."""
        import re

        match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
        if not match:
            return 0
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        return hours * 3600 + minutes * 60 + seconds

    def get_video_analytics(
        self, youtube_video_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Fetch per-video analytics from the YouTube Analytics API.

        Returns a dict keyed by ``youtube_video_id`` with sub-keys
        ``avg_percentage_viewed``, ``avg_view_duration_seconds``,
        ``estimated_minutes_watched``.

        Returns an empty dict if the analytics client is unavailable.
        """
        if not self._youtube_analytics:
            return {}

        analytics: dict[str, dict[str, Any]] = {}
        today = now_ist().strftime("%Y-%m-%d")

        # Batch by 40 IDs to stay within filter-string limits.
        for i in range(0, len(youtube_video_ids), 40):
            batch = youtube_video_ids[i : i + 40]
            try:
                response = (
                    self._youtube_analytics.reports()
                    .query(
                        ids="channel==MINE",
                        startDate="2005-01-01",
                        endDate=today,
                        dimensions="video",
                        metrics="averageViewPercentage,averageViewDuration,estimatedMinutesWatched",
                        filters=f"video=={','.join(batch)}",
                        maxResults=200,
                    )
                    .execute()
                )
            except Exception as exc:
                logger.warning(
                    "YouTube Analytics query failed: %s — skipping analytics for this batch.",
                    exc,
                )
                continue

            headers = [h["name"] for h in response.get("columnHeaders", [])]
            for row in response.get("rows", []):
                row_dict = dict(zip(headers, row))
                vid = row_dict.get("video")
                if vid:
                    analytics[vid] = {
                        "avg_percentage_viewed": round(
                            float(row_dict.get("averageViewPercentage", 0)), 2
                        ),
                        "avg_view_duration_seconds": round(
                            float(row_dict.get("averageViewDuration", 0))
                        ),
                        "estimated_minutes_watched": round(
                            float(row_dict.get("estimatedMinutesWatched", 0)), 1
                        ),
                    }

        return analytics

    def get_subscribers_gained(
        self, youtube_video_ids: list[str]
    ) -> dict[str, int]:
        """Fetch subscribers gained per video from the YouTube Analytics API.

        Returns a dict keyed by ``youtube_video_id`` with the number of
        subscribers gained by each video. Returns an empty dict if the
        analytics client is unavailable.
        """
        if not self._youtube_analytics:
            return {}

        result: dict[str, int] = {}
        today = now_ist().strftime("%Y-%m-%d")

        for i in range(0, len(youtube_video_ids), 40):
            batch = youtube_video_ids[i : i + 40]
            try:
                response = (
                    self._youtube_analytics.reports()
                    .query(
                        ids="channel==MINE",
                        startDate="2005-01-01",
                        endDate=today,
                        dimensions="video",
                        metrics="subscribersGained",
                        filters=f"video=={','.join(batch)}",
                        maxResults=200,
                    )
                    .execute()
                )
            except Exception as exc:
                logger.warning(
                    "YouTube Analytics subscribersGained query failed: %s", exc
                )
                continue

            headers = [h["name"] for h in response.get("columnHeaders", [])]
            for row in response.get("rows", []):
                row_dict = dict(zip(headers, row))
                vid = row_dict.get("video")
                if vid:
                    result[vid] = int(row_dict.get("subscribersGained", 0))

        return result

    def get_video_stats(
        self, youtube_video_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Fetch views, likes, comments, duration, derived rates, and analytics.

        Returns a dict keyed by ``youtube_video_id`` with Data API stats
        merged with Analytics API metrics when available.
        """
        stats: dict[str, dict[str, Any]] = {}

        for i in range(0, len(youtube_video_ids), 50):
            batch = youtube_video_ids[i : i + 50]
            response = (
                self._youtube.videos()
                .list(
                    part="statistics,contentDetails",
                    id=",".join(batch),
                )
                .execute()
            )
            for item in response.get("items", []):
                s = item["statistics"]
                content = item.get("contentDetails", {})

                views = int(s.get("viewCount", 0))
                likes = int(s.get("likeCount", 0))
                comments = int(s.get("commentCount", 0))
                duration_seconds = self._parse_iso8601_duration(
                    content.get("duration")
                )

                if views > 0:
                    engagement_rate = round((likes + comments) / views * 100, 4)
                    like_rate = round(likes / views * 100, 4)
                    comment_rate = round(comments / views * 100, 4)
                else:
                    engagement_rate = like_rate = comment_rate = 0.0

                stats[item["id"]] = {
                    "views": views,
                    "likes": likes,
                    "comments": comments,
                    "duration_seconds": duration_seconds,
                    "engagement_rate": engagement_rate,
                    "like_rate": like_rate,
                    "comment_rate": comment_rate,
                }

        # Merge analytics data (avg_percentage_viewed, avg_view_duration, etc.)
        analytics = self.get_video_analytics(youtube_video_ids)
        for vid, adata in analytics.items():
            if vid in stats:
                stats[vid].update(adata)
            else:
                stats[vid] = adata

        return stats

    def upload_video(
        self,
        file_path: str,
        title: str,
        description: str,
        tags: list[str],
        category_id: str = "22",  # "People & Blogs" default
        publish_at: str | None = None,
    ) -> str:
        """Upload a video file to YouTube via resumable upload.

        Parameters
        ----------
        publish_at:
            ISO 8601 datetime in UTC (e.g. ``"2026-03-10T04:30:00Z"``).
            When provided the video is uploaded as ``private`` with a
            ``publishAt`` timestamp so YouTube auto-publishes it at the
            given time.

        Returns the ``youtube_video_id`` of the newly created video.
        """
        video_status: dict = {"privacyStatus": "private"}
        if publish_at:
            video_status["publishAt"] = publish_at

        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": category_id,
            },
            "status": video_status,
        }

        media = MediaFileUpload(
            file_path,
            mimetype="video/mp4",
            resumable=True,
            chunksize=10 * 1024 * 1024,  # 10 MB chunks
        )

        request = self._youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        response = None
        while response is None:
            _, response = request.next_chunk()

        return response["id"]


class YouTubeServiceManager:
    """Manages per-channel YouTubeService instances.

    Reads OAuth tokens from the ``channels`` collection in MongoDB
    and caches ``YouTubeService`` instances.  Client credentials come
    from the DB ``config`` collection (with an ``.env`` fallback).
    """

    def __init__(
        self,
        db: Any,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        self._db = db
        self._client_id = client_id
        self._client_secret = client_secret
        self._cache: dict[str, YouTubeService] = {}

    async def _resolve_credentials(self) -> tuple[str, str]:
        """Return (client_id, client_secret) from DB config or constructor fallback."""
        from app.database import get_youtube_oauth_config

        cfg = await get_youtube_oauth_config(self._db)
        cid = (cfg or {}).get("client_id") or self._client_id
        csecret = (cfg or {}).get("client_secret") or self._client_secret
        if not cid or not csecret:
            raise RuntimeError(
                "YouTube OAuth client credentials are not configured. "
                "Set them via PUT /api/v1/channels/config/youtube-oauth or in .env"
            )
        return cid, csecret

    async def get_service(self, channel_id: str) -> YouTubeService | None:
        """Return the YouTubeService for *channel_id*, or ``None`` if no token exists."""
        if channel_id in self._cache:
            return self._cache[channel_id]

        channel = await self._db.channels.find_one({"channel_id": channel_id})
        if not channel or not channel.get("youtube_tokens"):
            logger.warning(
                "No YouTube tokens stored for channel '%s'",
                channel_id,
            )
            return None

        try:
            client_id, client_secret = await self._resolve_credentials()
            service = YouTubeService(
                client_id=client_id,
                client_secret=client_secret,
                token_data=channel["youtube_tokens"],
                db=self._db,
                channel_id=channel_id,
            )
            self._cache[channel_id] = service
            logger.info("YouTube service initialised for channel '%s'", channel_id)
            return service
        except Exception:
            logger.exception("Failed to initialise YouTube service for channel '%s'", channel_id)
            return None

    async def has_token(self, channel_id: str) -> bool:
        """Check if YouTube tokens exist for *channel_id* in the DB."""
        channel = await self._db.channels.find_one(
            {"channel_id": channel_id, "youtube_tokens": {"$exists": True, "$ne": None}},
            {"_id": 1},
        )
        return channel is not None

    def invalidate(self, channel_id: str) -> None:
        """Remove a cached service instance (e.g. after token update)."""
        self._cache.pop(channel_id, None)
