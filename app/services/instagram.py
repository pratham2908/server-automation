"""Instagram Graph API service.

Wraps the Instagram Graph API (accessed via Facebook) to fetch account
info, list reels, and retrieve per-reel insights.  Tokens are stored in
the MongoDB ``channels`` collection and refreshed automatically.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import requests
from pydantic import BaseModel

from app.logger import get_logger
from app.services.metrics import metrics_service
from app.timezone import now_ist

logger = get_logger(__name__)

# Two integrations run in parallel during the migration:
#   facebook  – Instagram Graph API via Facebook Login (graph.facebook.com)
#   instagram – Instagram API with Instagram Login   (graph.instagram.com)
PROVIDER_FACEBOOK = "facebook"
PROVIDER_INSTAGRAM = "instagram"

_FB_GRAPH_BASE = "https://graph.facebook.com/v25.0"
_IG_GRAPH_BASE = "https://graph.instagram.com/v23.0"

# Back-compat alias — Facebook host used by debug_token (a Facebook-only endpoint).
_GRAPH_BASE = _FB_GRAPH_BASE


def _base_for(provider: str) -> str:
    return _IG_GRAPH_BASE if provider == PROVIDER_INSTAGRAM else _FB_GRAPH_BASE


class TokenCheck(BaseModel):
    """Result of a live Graph introspection of an Instagram/Facebook token."""

    reachable: bool  # did the Graph call complete (vs a network/other failure)?
    valid: bool  # is the token currently usable right now?
    expires_at: int | None = None  # unix seconds; 0 means "never expires"; None = unknown
    error: str | None = None


def _introspect_instagram_login(token: str) -> TokenCheck:
    """Validate an Instagram-Login token via ``graph.instagram.com/me``.

    ``debug_token`` is a Facebook-only endpoint, so Instagram-Login tokens are
    checked by a lightweight ``/me`` call: an ``id`` means valid; an ``error``
    (code 190) means expired/invalid. ``/me`` returns no expiry, so ``expires_at``
    stays ``None`` and callers keep the stored value.
    """
    try:
        resp = requests.get(
            f"{_IG_GRAPH_BASE}/me",
            params={"fields": "id,username", "access_token": token},
            timeout=15,
        )
        body = resp.json()
    except Exception as e:  # network / non-JSON — cannot determine validity
        logger.warning("Instagram Login /me unreachable: %s", e)
        return TokenCheck(reachable=False, valid=False, error=str(e))

    if body.get("id"):
        return TokenCheck(reachable=True, valid=True, expires_at=None)
    err = body.get("error") or {}
    if err.get("code") == 190:
        return TokenCheck(reachable=True, valid=False, error=err.get("message"))
    logger.warning("Instagram Login /me inconclusive: %s", err or body)
    return TokenCheck(reachable=False, valid=False, error=err.get("message") or "inconclusive")


def introspect_token(
    token: str,
    app_id: str | None = None,
    app_secret: str | None = None,
    provider: str = PROVIDER_FACEBOOK,
) -> TokenCheck:
    """Introspect *token* to learn its real validity/expiry.

    ``instagram`` provider tokens are validated via ``graph.instagram.com/me``.
    ``facebook`` provider tokens use Graph ``debug_token`` — with the app access
    token (``app_id|app_secret``) when available, else self-debugged with the
    token itself (a live token returns ``data``; an expired one a top-level
    ``OAuthException`` code 190).

    Blocking I/O (``requests``) — call via ``asyncio.to_thread`` from async code.
    """
    if provider == PROVIDER_INSTAGRAM:
        return _introspect_instagram_login(token)

    verifier = f"{app_id}|{app_secret}" if app_id and app_secret else token
    try:
        resp = requests.get(
            f"{_GRAPH_BASE}/debug_token",
            params={"input_token": token, "access_token": verifier},
            timeout=15,
        )
        body = resp.json()
    except Exception as e:  # network error / non-JSON — cannot determine validity
        logger.warning("Instagram debug_token unreachable: %s", e)
        return TokenCheck(reachable=False, valid=False, error=str(e))

    data = body.get("data")
    if isinstance(data, dict):
        # App-token debug of an expired token also lands here with is_valid=False.
        inner_err = data.get("error")
        return TokenCheck(
            reachable=True,
            valid=bool(data.get("is_valid")),
            expires_at=data.get("expires_at"),
            error=inner_err.get("message") if isinstance(inner_err, dict) else None,
        )

    err = body.get("error") or {}
    if err.get("code") == 190:  # self-debug of an expired/invalid token
        return TokenCheck(reachable=True, valid=False, error=err.get("message"))

    # Anything else (rate limit, app-token required, etc.) — inconclusive, not authoritative.
    logger.warning("Instagram debug_token inconclusive: %s", err or body)
    return TokenCheck(reachable=False, valid=False, error=err.get("message") or "inconclusive")


class InstagramService:
    """Wraps the Instagram Graph API for a single channel."""

    def __init__(
        self,
        access_token: str,
        *,
        provider: str = PROVIDER_FACEBOOK,
        db: Any = None,
        channel_id: str | None = None,
    ) -> None:
        # Strip whitespace/newlines — a pasted token with a trailing "\n" produces
        # an "Invalid header value" (Bearer <token>\n) and fails every Graph call.
        self._token = access_token.strip()
        self._provider = provider
        self._base = _base_for(provider)
        self._db = db
        self._channel_id = channel_id

    def _get(self, endpoint: str, params: dict | None = None) -> dict:

        headers = {"Authorization": f"Bearer {self._token}"}
        start_time = time.time()
        try:
            resp = requests.get(
                f"{self._base}/{endpoint}",
                params=params,
                headers=headers,
                timeout=30,
            )
            duration = (time.time() - start_time) * 1000

            if not resp.ok:
                metrics_service.record_external_call("instagram", duration, False)
                try:
                    error_data = resp.json()
                    logger.error("Instagram API GET failed (%d): %s", resp.status_code, error_data)
                except Exception:
                    logger.error("Instagram API GET failed (%d): %s", resp.status_code, resp.text)
            else:
                metrics_service.record_external_call("instagram", duration, True)

            resp.raise_for_status()
            from typing import cast

            return cast(dict, resp.json())
        except Exception as e:
            if not isinstance(e, requests.HTTPError):
                duration = (time.time() - start_time) * 1000
                metrics_service.record_external_call("instagram", duration, False)
            raise e

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

    def _require_business_discovery(self) -> None:
        """business_discovery is a Facebook-Login-only feature — it does not exist on
        the Instagram Login API. Fail loudly instead of 400-ing graph.instagram.com."""
        if self._provider == PROVIDER_INSTAGRAM:
            raise ValueError(
                "business_discovery (competitor lookup) is not available on the Instagram "
                "Login API; competitor intel requires a Facebook-Login token."
            )

    def discover_business_account(self, own_ig_user_id: str, target_username: str) -> dict[str, Any]:
        """Fetch metadata for *any* Business/Creator account using Business Discovery.

        Requires an authenticated business account (own_ig_user_id) to perform
        the search.  Returns a dict with basic metadata.
        """
        self._require_business_discovery()
        query = f"business_discovery.username({target_username}){{id,username,name,profile_picture_url,followers_count,media_count,biography}}"
        data = self._get(own_ig_user_id, {"fields": query})

        disc = data.get("business_discovery", {})
        return {
            "instagram_user_id": disc.get("id"),
            "username": disc.get("username", target_username),
            "name": disc.get("name", ""),
            "profile_picture_url": disc.get("profile_picture_url", ""),
            "followers_count": disc.get("followers_count", 0),
            "media_count": disc.get("media_count", 0),
            "biography": disc.get("biography", ""),
        }

    def discover_competitor_media(
        self, own_ig_user_id: str, target_username: str, max_results: int = 50
    ) -> list[dict[str, Any]]:
        """Fetch recent reels/videos from *any* Business account using Business Discovery.

        Note: The Business Discovery API has strict limitations on pagination depth.
        """
        self._require_business_discovery()
        fields = (
            f"business_discovery.username({target_username})"
            f"{{media{{id,caption,media_type,media_url,timestamp,permalink,like_count,comments_count}}}}"
        )

        try:
            data = self._get(own_ig_user_id, {"fields": fields})
            media_list = data.get("business_discovery", {}).get("media", {}).get("data", [])

            reels: list[dict[str, Any]] = []
            for item in media_list:
                if item.get("media_type") in ("VIDEO", "REEL"):
                    reels.append(
                        {
                            "id": item.get("id"),
                            "caption": item.get("caption", ""),
                            "permalink": item.get("permalink", ""),
                            "published_at": item.get("timestamp", ""),
                            "like_count": int(item.get("like_count", 0)),
                            "comment_count": int(item.get("comments_count", 0)),
                            "views": 0,  # Business Discovery does NOT provide view counts for public media
                        }
                    )
                if len(reels) >= max_results:
                    break
            return reels
        except Exception as exc:
            logger.error("Business Discovery media fetch failed for %s: %s", target_username, exc)
            return []

    # ------------------------------------------------------------------
    # Reels
    # ------------------------------------------------------------------

    def get_reels(self, ig_user_id: str) -> list[dict[str, Any]]:
        """Fetch all reels (VIDEO / REEL media) with basic metrics.

        Paginates through ``/{ig_user_id}/media`` and filters by
        ``media_type`` to keep only video/reel content.
        """
        fields = "id,caption,media_type,media_url,thumbnail_url,timestamp,permalink,like_count,comments_count"
        reels: list[dict[str, Any]] = []
        url: str | None = f"{ig_user_id}/media"
        params: dict = {"fields": fields, "limit": "100"}

        while url:
            body = self._get(url, params=params)
            for item in body.get("data", []):
                if item.get("media_type") in ("VIDEO", "REEL"):
                    reels.append(item)
            paging = body.get("paging", {})
            next_url = paging.get("next")
            if next_url:
                # The 'next' URL is absolute and contains all tokens,
                # but our _get helper prepends _GRAPH_BASE.
                # So we strip the base if it's there, or just use requests.get directly for paging.
                params = {}
                url = next_url.replace(f"{self._base}/", "")
            else:
                url = None

        logger.info("Fetched %d reels for IG user %s", len(reels), ig_user_id)
        return reels

    def get_reel_media_url(self, media_id: str) -> str:
        """Return a time-limited CDN URL for the reel/video binary (Graph API).

        Used when copying a published reel into R2 for repost or re-upload.
        """
        data = self._get(media_id, {"fields": "media_url,media_type"})
        if data.get("media_type") not in ("VIDEO", "REEL"):
            raise ValueError("Media is not a video or reel")
        url = data.get("media_url")
        if not url:
            raise ValueError("Instagram did not return media_url for this media")
        return str(url)

    # ------------------------------------------------------------------
    # Comment fetching
    # ------------------------------------------------------------------

    def get_media_comments(self, media_id: str) -> list[dict[str, Any]]:
        """Fetch all comments on a media item owned by the authenticated account.

        Returns a list of dicts with keys:
        ``comment_id``, ``text``, ``like_count``, ``author``, ``published_at``.
        """
        fields = "id,text,timestamp,like_count,username"
        comments: list[dict[str, Any]] = []
        url: str | None = f"{media_id}/comments"
        params: dict = {"fields": fields, "limit": "100"}

        while url:
            body = self._get(url, params=params)
            for item in body.get("data", []):
                comments.append(
                    {
                        "comment_id": item.get("id", ""),
                        "text": item.get("text", ""),
                        "like_count": int(item.get("like_count", 0)),
                        "author": item.get("username", ""),
                        "published_at": item.get("timestamp", ""),
                        "video_url": f"https://www.instagram.com/reels/{media_id}/",
                        "comment_url": f"https://www.instagram.com/reels/comments/{item.get('id', '')}/",
                    }
                )
            paging = body.get("paging", {})
            next_url = paging.get("next")
            if next_url:
                params = {}
                url = next_url.replace(f"{self._base}/", "")
            else:
                url = None

        return comments

    def get_media_comments_since(
        self,
        media_id: str,
        cutoff_timestamp: str | datetime,
    ) -> list[dict[str, Any]]:
        """Fetch comments newer than *cutoff_timestamp*.

        The Instagram API does not support server-side time filtering,
        so this fetches all comments and filters client-side.
        """
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        if isinstance(cutoff_timestamp, str):
            cutoff = _dt.fromisoformat(cutoff_timestamp.replace("Z", "+00:00"))
        else:
            cutoff = cutoff_timestamp
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=_tz.utc)

        all_comments = self.get_media_comments(media_id)
        new_comments: list[dict[str, Any]] = []
        for c in all_comments:
            try:
                pub = _dt.fromisoformat(c["published_at"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                new_comments.append(c)
                continue
            if pub > cutoff:
                new_comments.append(c)

        return new_comments

    def reply_to_comment(self, comment_id: str, message: str) -> str:
        """Reply to a comment on an owned media item.

        Requires ``instagram_manage_comments`` permission.
        Returns the ID of the newly created reply.
        """
        from typing import cast

        return cast(str, self._post(f"{comment_id}/replies", {"message": message}).get("id", ""))

    def get_reel_insights(self, media_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch per-reel insights (views, reach, saved, shares).

        Returns a dict keyed by ``media_id``.
        """
        insights: dict[str, dict[str, Any]] = {}
        metrics = "views,reach,saved,shares,total_interactions"

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
        import time

        from app.services.metrics import metrics_service

        payload = params or {}
        headers = {"Authorization": f"Bearer {self._token}"}
        start_time = time.time()
        try:
            resp = requests.post(
                f"{self._base}/{endpoint}",
                data=payload,
                headers=headers,
                timeout=60,
            )
            duration = (time.time() - start_time) * 1000

            if not resp.ok:
                metrics_service.record_external_call("instagram", duration, False)
                try:
                    error_data = resp.json()
                    logger.error("Instagram API POST failed (%d): %s", resp.status_code, error_data)
                except Exception:
                    logger.error("Instagram API POST failed (%d): %s", resp.status_code, resp.text)
            else:
                metrics_service.record_external_call("instagram", duration, True)

            resp.raise_for_status()
            from typing import cast

            return cast(dict, resp.json())
        except Exception as e:
            if not isinstance(e, requests.HTTPError):
                duration = (time.time() - start_time) * 1000
                metrics_service.record_external_call("instagram", duration, False)
            raise e

    def create_reel_container(
        self,
        ig_user_id: str,
        caption: str,
        *,
        upload_type: str = "resumable",
        thumb_offset: int | None = None,
    ) -> dict[str, str]:
        """Create a Reel media container for resumable upload.

        Returns ``{"container_id": "...", "upload_uri": "..."}``.
        """
        params: dict[str, Any] = {
            "media_type": "REELS",
            "upload_type": upload_type,
            "caption": caption,
        }
        if thumb_offset is not None:
            params["thumb_offset"] = str(thumb_offset)

        data = self._post(f"{ig_user_id}/media", params)
        container_id = data.get("id", "")
        upload_uri = data.get("uri", "")
        logger.info(
            "Created reel container %s for IG user %s",
            container_id,
            ig_user_id,
        )
        return {"container_id": container_id, "upload_uri": upload_uri}

    def upload_video_to_container(self, upload_uri: str, file_path: str) -> None:
        """Stream a video file to the Instagram resumable upload endpoint."""
        import os

        file_size = os.path.getsize(file_path)
        # Use both standard and X-Entity headers for maximum compatibility with rupload
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Offset": "0",
            "X-Entity-Length": str(file_size),
            "X-Entity-Name": f"reels_upload_{int(time.time())}_{os.path.basename(file_path)}",
            "X-Entity-Type": "video/mp4",
            "Content-Type": "application/octet-stream",
        }

        # Some versions of the IG API prefer these simpler headers
        headers.update(
            {
                "offset": "0",
                "file_size": str(file_size),
            }
        )

        # Read file into memory to avoid 'Transfer-Encoding: chunked' issues with Instagram
        with open(file_path, "rb") as f:
            binary_data = f.read()

        logger.info("Uploading %d bytes to Instagram rupload...", file_size)
        try:
            resp = requests.post(
                upload_uri,
                headers=headers,
                data=binary_data,
                timeout=600,
            )

            if not resp.ok:
                logger.error("Instagram rupload failed (%d): %s", resp.status_code, resp.text)
                try:
                    error_json = resp.json()
                    logger.error("Instagram rupload error JSON: %s", json.dumps(error_json))
                except Exception:
                    pass

            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            logger.error("HTTP error during Instagram upload: %s", e)
            raise e
        except Exception as e:
            logger.error("Unexpected error during Instagram upload: %s", e)
            raise e

        logger.info("Uploaded video (%d bytes) to %s", file_size, upload_uri[:80])

    def get_container_status(self, container_id: str) -> tuple[str, str]:
        """Poll container processing status, with Meta's diagnostic detail.

        Returns ``(status_code, status)``. ``status_code`` is the machine
        state (``FINISHED`` / ``IN_PROGRESS`` / ``ERROR`` / ``EXPIRED``);
        ``status`` is the human-readable description, which is the only place
        Meta explains *why* processing failed. Requesting only ``status_code``
        used to discard that reason, leaving "processing failed (status:
        ERROR)" with nothing to act on.
        """
        data = self._get(container_id, {"fields": "status_code,status"})
        return str(data.get("status_code", "UNKNOWN")), str(data.get("status", ""))

    def check_container_status(self, container_id: str) -> str:
        """Poll container processing status, returning just the status code."""
        return self.get_container_status(container_id)[0]

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
        from typing import cast

        return cast(str, media_id)

    def publish_reel(
        self,
        ig_user_id: str,
        file_path: str,
        caption: str,
        *,
        poll_interval: float = 5.0,
        max_polls: int = 60,
    ) -> str:
        """End-to-end reel publish using resumable upload (local file)."""
        import time

        container = self.create_reel_container(ig_user_id, caption)
        cid = container["container_id"]
        uri = container["upload_uri"]

        self.upload_video_to_container(uri, file_path)

        for _ in range(max_polls):
            st, detail = self.get_container_status(cid)
            if st == "FINISHED":
                break
            if st == "ERROR":
                raise RuntimeError(
                    f"Instagram container {cid} processing failed"
                    + (f": {detail}" if detail else " (Meta gave no reason)")
                )
            time.sleep(poll_interval)
        else:
            raise TimeoutError(f"Instagram container {cid} not ready after {max_polls * poll_interval}s")

        return self.publish_container(ig_user_id, cid)

    def publish_reel_from_url(
        self,
        ig_user_id: str,
        video_url: str,
        caption: str,
        *,
        thumb_offset: int | None = None,
        poll_interval: float = 10.0,
        max_polls: int = 40,
    ) -> str:
        """End-to-end reel publish using a public video URL.

        This is often more robust than resumable upload for files already in the cloud.
        """
        import time

        # 1. Create container with video_url
        params: dict[str, Any] = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
        }
        if thumb_offset is not None:
            params["thumb_offset"] = str(thumb_offset)

        data = self._post(f"{ig_user_id}/media", params)
        cid = data.get("id", "")
        if not cid:
            raise RuntimeError(f"Failed to create media container: {data}")

        logger.info("Created Instagram Reel container %s from URL", cid)

        # 2. Wait for processing
        for i in range(max_polls):
            st, detail = self.get_container_status(cid)
            if st == "FINISHED":
                logger.info("Container %s processing FINISHED", cid)
                break
            if st == "ERROR":
                raise RuntimeError(
                    f"Instagram container {cid} processing failed"
                    + (f": {detail}" if detail else " (Meta gave no reason)")
                )

            if i % 3 == 0:
                logger.info("Waiting for container %s processing... (status: %s)", cid, st)
            time.sleep(poll_interval)
        else:
            raise TimeoutError(f"Instagram container {cid} not ready after {max_polls * poll_interval}s")

        # 3. Publish
        return self.publish_container(ig_user_id, cid)

    # ------------------------------------------------------------------
    # Token refresh
    # ------------------------------------------------------------------

    async def refresh_token(self, app_id: str, app_secret: str) -> str | None:
        """Exchange the current long-lived token for a new one (60-day window).

        Returns the new token string, or ``None`` on failure.
        """
        try:
            if self._provider == PROVIDER_INSTAGRAM:
                # Instagram Login: refresh a long-lived IG token (no app secret needed).
                resp = requests.get(
                    f"{_IG_GRAPH_BASE}/refresh_access_token",
                    params={"grant_type": "ig_refresh_token", "access_token": self._token},
                    timeout=30,
                )
            else:
                resp = requests.get(
                    f"{_FB_GRAPH_BASE}/oauth/access_token",
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
                expires_in = data.get("expires_in", 5184000)
                expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

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

                await _save()
                self._token = new_token
                logger.info("Refreshed Instagram token for channel '%s'", self._channel_id)
            return cast(str, new_token)
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
            tokens = channel["instagram_tokens"]
            service = InstagramService(
                access_token=tokens["access_token"],
                provider=tokens.get("provider", PROVIDER_FACEBOOK),
                db=self._db,
                channel_id=channel_id,
            )
            self._cache[channel_id] = service
            logger.info("Instagram service initialised for channel '%s'", channel_id)
            return service
        except Exception:
            logger.exception("Failed to init Instagram service for channel '%s'", channel_id)
            return None

    def invalidate(self, channel_id: str) -> None:
        self._cache.pop(channel_id, None)
