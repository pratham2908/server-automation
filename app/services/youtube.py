from __future__ import annotations

"""YouTube Data API v3 service – stats fetching and resumable upload.

Authentication uses a stored OAuth2 token (initially created via browser-based
consent flow; refreshed automatically thereafter).
"""

from typing import Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Read-write scope required for uploading videos.
_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


class YouTubeService:
    """Wraps the YouTube Data API for stats retrieval and video upload."""

    def __init__(
        self,
        client_secret_path: str,
        token_path: str,
    ) -> None:
        self._client_secret_path = client_secret_path
        self._token_path = token_path
        self._youtube = self._build_client()

    # ------------------------------------------------------------------
    # Client bootstrap
    # ------------------------------------------------------------------

    def _build_client(self) -> Any:
        """Build (and cache) an authorised YouTube API client."""
        creds = self._load_or_refresh_credentials()
        return build("youtube", "v3", credentials=creds)

    def _load_or_refresh_credentials(self) -> Credentials:
        """Load credentials from the token file, refreshing if needed.

        Falls back to the full OAuth consent flow when no token exists yet
        (only relevant during initial server setup).
        """
        import json
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
        flow = InstalledAppFlow.from_client_secrets_file(
            self._client_secret_path, _SCOPES
        )
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

    def get_video_stats(
        self, youtube_video_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Fetch views, likes, and comments for a batch of videos.

        Returns a dict keyed by ``youtube_video_id`` with sub-keys
        ``views``, ``likes``, ``comments``.
        """
        stats: dict[str, dict[str, Any]] = {}

        # YouTube allows up to 50 IDs per request.
        for i in range(0, len(youtube_video_ids), 50):
            batch = youtube_video_ids[i : i + 50]
            response = (
                self._youtube.videos()
                .list(part="statistics", id=",".join(batch))
                .execute()
            )
            for item in response.get("items", []):
                s = item["statistics"]
                stats[item["id"]] = {
                    "views": int(s.get("viewCount", 0)),
                    "likes": int(s.get("likeCount", 0)),
                    "comments": int(s.get("commentCount", 0)),
                }

        return stats

    def upload_video(
        self,
        file_path: str,
        title: str,
        description: str,
        tags: list[str],
        category_id: str = "22",  # "People & Blogs" default
    ) -> str:
        """Upload a video file to YouTube via resumable upload.

        Returns the ``youtube_video_id`` of the newly created video.
        """
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": category_id,
            },
            "status": {"privacyStatus": "private"},
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
