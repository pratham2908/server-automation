from __future__ import annotations

"""Instagram Graph API service.

Wraps the Instagram Graph API (accessed via Facebook) to fetch account
info, list reels, and retrieve per-reel insights.  Tokens are stored in
the MongoDB ``channels`` collection and refreshed automatically.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from app.logger import get_logger
from app.timezone import now_ist

logger = get_logger(__name__)

_GRAPH_BASE = "https://graph.facebook.com/v21.0"


class InstagramService:
    """Wraps the Instagram Graph API for a single channel."""

    def __init__(
        self,
        access_token: str,
        *,
        db: Any = None,
        channel_id: str | None = None,
    ) -> None:
        self._token = access_token
        self._db = db
        self._channel_id = channel_id

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        params = params or {}
        params["access_token"] = self._token
        resp = requests.get(f"{_GRAPH_BASE}/{endpoint}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Account info
    # ------------------------------------------------------------------

    def get_account_info(self, ig_user_id: str) -> dict[str, Any]:
        """Fetch Instagram Business/Creator account metadata."""
        fields = "id,username,name,profile_picture_url,followers_count,media_count,biography"
        data = self._get(ig_user_id, {"fields": fields})
        return {
            "instagram_user_id": data.get("id", ig_user_id),
            "username": data.get("username", ""),
            "name": data.get("name", ""),
            "profile_picture_url": data.get("profile_picture_url", ""),
            "followers_count": data.get("followers_count", 0),
            "media_count": data.get("media_count", 0),
            "biography": data.get("biography", ""),
        }

    # ------------------------------------------------------------------
    # Reels
    # ------------------------------------------------------------------

    def get_reels(self, ig_user_id: str) -> list[dict[str, Any]]:
        """Fetch all reels (VIDEO / REEL media) with basic metrics.

        Paginates through ``/{ig_user_id}/media`` and filters by
        ``media_type`` to keep only video/reel content.
        """
        fields = (
            "id,caption,media_type,media_url,thumbnail_url,"
            "timestamp,permalink,like_count,comments_count"
        )
        reels: list[dict[str, Any]] = []
        url: str | None = f"{_GRAPH_BASE}/{ig_user_id}/media"
        params: dict = {"fields": fields, "limit": "100", "access_token": self._token}

        while url:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            body = resp.json()
            for item in body.get("data", []):
                if item.get("media_type") in ("VIDEO", "REEL"):
                    reels.append(item)
            paging = body.get("paging", {})
            url = paging.get("next")
            params = {}  # next URL already contains params

        logger.info("Fetched %d reels for IG user %s", len(reels), ig_user_id)
        return reels

    def get_reel_insights(self, media_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch per-reel insights (plays, reach, saved, shares).

        Returns a dict keyed by ``media_id``.
        """
        insights: dict[str, dict[str, Any]] = {}
        metrics = "plays,reach,saved,shares,total_interactions"

        for mid in media_ids:
            try:
                data = self._get(f"{mid}/insights", {"metric": metrics})
                row: dict[str, Any] = {}
                for entry in data.get("data", []):
                    name = entry.get("name")
                    values = entry.get("values", [{}])
                    row[name] = values[0].get("value", 0) if values else 0
                insights[mid] = row
            except Exception as exc:
                logger.warning("Could not fetch insights for media %s: %s", mid, exc)

        return insights

    # ------------------------------------------------------------------
    # Publishing (Reels)
    # ------------------------------------------------------------------

    def _post(self, endpoint: str, params: dict | None = None) -> dict:
        params = params or {}
        params["access_token"] = self._token
        resp = requests.post(f"{_GRAPH_BASE}/{endpoint}", params=params, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def create_reel_container(
        self,
        ig_user_id: str,
        caption: str,
        *,
        upload_type: str = "resumable",
    ) -> dict[str, str]:
        """Create a Reel media container for resumable upload.

        Returns ``{"container_id": "...", "upload_uri": "..."}``.
        """
        params: dict[str, str] = {
            "media_type": "REELS",
            "upload_type": upload_type,
            "caption": caption,
        }
        data = self._post(f"{ig_user_id}/media", params)
        container_id = data.get("id", "")
        upload_uri = data.get("uri", "")
        logger.info(
            "Created reel container %s for IG user %s", container_id, ig_user_id,
        )
        return {"container_id": container_id, "upload_uri": upload_uri}

    def upload_video_to_container(self, upload_uri: str, file_path: str) -> None:
        """Stream a video file to the Instagram resumable upload endpoint."""
        import os

        file_size = os.path.getsize(file_path)
        headers = {
            "Authorization": f"OAuth {self._token}",
            "offset": "0",
            "file_size": str(file_size),
        }
        with open(file_path, "rb") as f:
            resp = requests.post(
                upload_uri,
                headers=headers,
                data=f,
                timeout=600,
            )
        resp.raise_for_status()
        logger.info("Uploaded video (%d bytes) to %s", file_size, upload_uri[:80])

    def check_container_status(self, container_id: str) -> str:
        """Poll container processing status.

        Returns the ``status_code`` string (e.g. ``"FINISHED"``,
        ``"IN_PROGRESS"``, ``"ERROR"``).
        """
        data = self._get(container_id, {"fields": "status_code"})
        return data.get("status_code", "UNKNOWN")

    def publish_container(self, ig_user_id: str, container_id: str) -> str:
        """Publish a processed container as a Reel.

        Returns the published ``media_id``.
        """
        data = self._post(
            f"{ig_user_id}/media_publish",
            {"creation_id": container_id},
        )
        media_id = data.get("id", "")
        logger.success("Published reel %s for IG user %s", media_id, ig_user_id)
        return media_id

    def publish_reel(
        self,
        ig_user_id: str,
        file_path: str,
        caption: str,
        *,
        poll_interval: float = 5.0,
        max_polls: int = 60,
    ) -> str:
        """End-to-end reel publish: create container, upload, wait, publish.

        Returns the published ``media_id``.  Raises on timeout or error.
        """
        import time

        container = self.create_reel_container(ig_user_id, caption)
        cid = container["container_id"]
        uri = container["upload_uri"]

        self.upload_video_to_container(uri, file_path)

        for _ in range(max_polls):
            st = self.check_container_status(cid)
            if st == "FINISHED":
                break
            if st == "ERROR":
                raise RuntimeError(f"Instagram container {cid} processing failed")
            time.sleep(poll_interval)
        else:
            raise TimeoutError(
                f"Instagram container {cid} not ready after {max_polls * poll_interval}s"
            )

        return self.publish_container(ig_user_id, cid)

    # ------------------------------------------------------------------
    # Token refresh
    # ------------------------------------------------------------------

    def refresh_token(self, app_id: str, app_secret: str) -> str | None:
        """Exchange the current long-lived token for a new one (60-day window).

        Returns the new token string, or ``None`` on failure.
        """
        try:
            resp = requests.get(
                f"{_GRAPH_BASE}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "fb_exchange_token": self._token,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            new_token = data.get("access_token")
            if new_token and self._db is not None and self._channel_id:
                import asyncio

                expires_in = data.get("expires_in", 5184000)
                expires_at = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=expires_in)
                ).isoformat()

                async def _save() -> None:
                    await self._db.channels.update_one(
                        {"channel_id": self._channel_id},
                        {
                            "$set": {
                                "instagram_tokens.access_token": new_token,
                                "instagram_tokens.expires_at": expires_at,
                                "updated_at": now_ist(),
                            }
                        },
                    )

                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(_save())
                except RuntimeError:
                    asyncio.run(_save())

                self._token = new_token
                logger.info("Refreshed Instagram token for channel '%s'", self._channel_id)
            return new_token
        except Exception as exc:
            logger.warning("Instagram token refresh failed: %s", exc)
            return None


class InstagramServiceManager:
    """Manages per-channel InstagramService instances (mirrors YouTubeServiceManager)."""

    def __init__(
        self,
        db: Any,
        app_id: str | None = None,
        app_secret: str | None = None,
    ) -> None:
        self._db = db
        self._app_id = app_id
        self._app_secret = app_secret
        self._cache: dict[str, InstagramService] = {}

    async def _resolve_credentials(self) -> tuple[str, str]:
        from app.database import get_instagram_oauth_config

        cfg = await get_instagram_oauth_config(self._db)
        aid = (cfg or {}).get("app_id") or self._app_id
        asecret = (cfg or {}).get("app_secret") or self._app_secret
        if not aid or not asecret:
            raise RuntimeError(
                "Instagram OAuth credentials not configured. "
                "Set them via PUT /api/v1/channels/config/instagram-oauth or in .env"
            )
        return aid, asecret

    async def get_service(self, channel_id: str) -> InstagramService | None:
        if channel_id in self._cache:
            return self._cache[channel_id]

        channel = await self._db.channels.find_one({"channel_id": channel_id})
        if not channel or not channel.get("instagram_tokens"):
            logger.warning("No Instagram tokens stored for channel '%s'", channel_id)
            return None

        try:
            service = InstagramService(
                access_token=channel["instagram_tokens"]["access_token"],
                db=self._db,
                channel_id=channel_id,
            )
            self._cache[channel_id] = service
            logger.info("Instagram service initialised for channel '%s'", channel_id)
            return service
        except Exception:
            logger.exception("Failed to init Instagram service for channel '%s'", channel_id)
            return None

    async def has_token(self, channel_id: str) -> bool:
        channel = await self._db.channels.find_one(
            {"channel_id": channel_id, "instagram_tokens": {"$exists": True, "$ne": None}},
            {"_id": 1},
        )
        return channel is not None

    def invalidate(self, channel_id: str) -> None:
        self._cache.pop(channel_id, None)
