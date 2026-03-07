from __future__ import annotations

"""YouTube Data API v3 + Analytics API service.

Authentication uses a stored OAuth2 token (initially created via browser-based
consent flow; refreshed automatically thereafter).
"""

from datetime import datetime
from typing import Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from app.logger import get_logger

logger = get_logger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


class YouTubeService:
    """Wraps the YouTube Data API and Analytics API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: str,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_path = token_path
        self._creds = self._load_or_refresh_credentials()
        self._youtube = self._build_data_client()
        self._youtube_analytics = self._build_analytics_client()

    # ------------------------------------------------------------------
    # Client bootstrap
    # ------------------------------------------------------------------

    def _build_data_client(self) -> Any:
        """Build an authorised YouTube Data API v3 client."""
        return build("youtube", "v3", credentials=self._creds)

    def _build_analytics_client(self) -> Any | None:
        """Build a YouTube Analytics API v2 client.

        Returns ``None`` if the token lacks the analytics scope (the caller
        should handle this gracefully).
        """
        try:
            return build("youtubeAnalytics", "v2", credentials=self._creds)
        except Exception as exc:
            logger.warning(
                "Could not build YouTube Analytics client: %s. "
                "Analytics metrics (avg_percentage_viewed, etc.) will be unavailable. "
                "Delete the token file and re-authenticate to add the analytics scope.",
                exc,
            )
            return None

    def _load_or_refresh_credentials(self) -> Credentials:
        """Load credentials from the token file, refreshing if needed.

        Falls back to the full OAuth consent flow when no token exists yet
        (only relevant during initial server setup).
        """
        import os

        if os.path.exists(self._token_path):
            creds = Credentials.from_authorized_user_file(
                self._token_path, _SCOPES
            )
            if creds and creds.valid:
                return creds
            if creds and creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request

                creds.refresh(Request())
                self._save_credentials(creds)
                return creds

        # First-time setup – requires interactive browser consent.
        client_config = {
            "installed": {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, _SCOPES)
        creds = flow.run_local_server(port=0)
        self._save_credentials(creds)
        return creds

    def _save_credentials(self, creds: Credentials) -> None:
        import json

        with open(self._token_path, "w") as f:
            f.write(creds.to_json())

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
        today = datetime.utcnow().strftime("%Y-%m-%d")

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
