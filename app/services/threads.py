"""Wraps Meta's Threads API for one connected Threads account.

Threads is its own Graph (``graph.threads.net``) with its own app id, secret and
OAuth: a Facebook or Instagram token can't call it, and its user ids are its own.
A channel's Threads token lives in ``threads_tokens`` on the channel doc.

Publishing is the Instagram shape — create a container from a public video URL,
wait for Meta to process it, then publish the container — so the publisher can
hand Threads the same presigned R2 link it hands Instagram.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any, cast

import requests

from app.logger import get_logger
from app.services.metrics import metrics_service
from app.services.threads_insights_parse import POST_METRICS
from app.timezone import now_ist

logger = get_logger(__name__)

GRAPH_BASE = "https://graph.threads.net/v1.0"
OAUTH_BASE = "https://graph.threads.net"
AUTHORIZE_URL = "https://threads.net/oauth/authorize"

# What we ask for at login. Insights, replies (for the first comment) and delete
# are optional scopes on Threads, so each feature that needs one is named here.
SCOPES = (
    "threads_basic",
    "threads_content_publish",
    "threads_manage_insights",
    "threads_manage_replies",
    "threads_read_replies",
    "threads_delete",
)

_PROFILE_FIELDS = "id,username,name,threads_profile_picture_url,threads_biography,is_verified"
_POST_FIELDS = "id,media_type,media_url,permalink,text,timestamp,shortcode,thumbnail_url,is_quote_post,topic_tag"
_TIMEOUT = 30


def _record(started: float, ok: bool) -> None:
    metrics_service.record_external_call("threads", (time.time() - started) * 1000, ok)


def _checked(resp: requests.Response, started: float, what: str) -> dict[str, Any]:
    """The JSON body, or raise with Meta's own message logged (the exception keeps the response)."""
    _record(started, resp.ok)
    if not resp.ok:
        logger.error("Threads API %s failed (%d): %s", what, resp.status_code, resp.text[:500])
    resp.raise_for_status()
    return cast(dict[str, Any], resp.json())


# ---------------------------------------------------------------------------
# OAuth — module level: these run before a channel has a token.
# ---------------------------------------------------------------------------


def exchange_code(app_id: str, app_secret: str, code: str, redirect_uri: str) -> dict[str, Any]:
    """Authorization code -> short-lived (1 h) token and the Threads user id.

    The redirect URI must match the authorize call exactly. A code is good once,
    for an hour; Threads appends ``#_`` to it in the redirect, which is stripped.
    """
    started = time.time()
    resp = requests.post(
        f"{OAUTH_BASE}/oauth/access_token",
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "code": code.split("#", 1)[0].strip(),
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=_TIMEOUT,
    )
    return _checked(resp, started, "code exchange")


def exchange_long_lived(app_secret: str, short_token: str) -> dict[str, Any]:
    """Short-lived token -> 60-day token: ``{access_token, token_type, expires_in}``."""
    started = time.time()
    resp = requests.get(
        f"{OAUTH_BASE}/access_token",
        params={"grant_type": "th_exchange_token", "client_secret": app_secret, "access_token": short_token},
        timeout=_TIMEOUT,
    )
    return _checked(resp, started, "long-lived exchange")


def refresh_long_lived(token: str) -> dict[str, Any]:
    """A fresh 60 days for a token at least a day old and not yet expired."""
    started = time.time()
    resp = requests.get(
        f"{OAUTH_BASE}/refresh_access_token",
        params={"grant_type": "th_refresh_token", "access_token": token},
        timeout=_TIMEOUT,
    )
    return _checked(resp, started, "token refresh")


def token_doc(body: dict[str, Any], user_id: str | None = None) -> dict[str, Any]:
    """The ``threads_tokens`` sub-document for a long-lived token reply."""
    now = now_ist()
    expires_in = body.get("expires_in")
    doc: dict[str, Any] = {
        "access_token": str(body["access_token"]).strip(),
        "token_type": body.get("token_type") or "bearer",
        "refreshed_at": now,
        "expires_at": (now + timedelta(seconds=int(expires_in))) if isinstance(expires_in, int | float) else None,
    }
    if user_id:
        doc["user_id"] = str(user_id)
    return doc


class ThreadsService:
    """Calls for one Threads account, addressed as ``me`` — a Threads token only ever reaches its own account."""

    def __init__(self, access_token: str) -> None:
        # A pasted token with a trailing newline makes an invalid header and fails every call.
        self._token = access_token.strip()

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.time()
        resp = requests.get(f"{GRAPH_BASE}/{path}", params=params, headers=self._headers, timeout=_TIMEOUT)
        return _checked(resp, started, f"GET {path}")

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        started = time.time()
        resp = requests.post(f"{GRAPH_BASE}/{path}", data=data, headers=self._headers, timeout=_TIMEOUT)
        return _checked(resp, started, f"POST {path}")

    def _delete(self, path: str) -> dict[str, Any]:
        started = time.time()
        resp = requests.delete(f"{GRAPH_BASE}/{path}", headers=self._headers, timeout=_TIMEOUT)
        return _checked(resp, started, f"DELETE {path}")

    # -- account ----------------------------------------------------------

    def get_profile(self) -> dict[str, Any]:
        """``{id, username, name, threads_profile_picture_url, threads_biography, is_verified}``."""
        return self._get("me", {"fields": _PROFILE_FIELDS})

    def publishing_limit(self) -> dict[str, Any]:
        """Posts used of the 250 a rolling day allows."""
        body = self._get("me/threads_publishing_limit", {"fields": "quota_usage,config"})
        rows = body.get("data") or []
        return rows[0] if rows else {}

    # -- publishing -------------------------------------------------------

    def create_video_container(self, video_url: str, text: str, topic: str | None = None) -> str:
        data: dict[str, Any] = {"media_type": "VIDEO", "video_url": video_url, "text": text}
        if topic:
            data["topic_tag"] = topic
        body = self._post("me/threads", data)
        container_id = str(body.get("id") or "")
        if not container_id:
            raise RuntimeError(f"Threads returned no container id: {body}")
        return container_id

    def container_status(self, container_id: str) -> tuple[str, str | None]:
        """``(status, error_message)``: IN_PROGRESS, FINISHED, PUBLISHED, ERROR or EXPIRED."""
        body = self._get(container_id, {"fields": "status,error_message"})
        return str(body.get("status") or ""), body.get("error_message")

    def publish_container(self, container_id: str) -> str:
        body = self._post("me/threads_publish", {"creation_id": container_id})
        media_id = str(body.get("id") or "")
        if not media_id:
            raise RuntimeError(f"Threads publish returned no media id: {body}")
        return media_id

    def publish_video(
        self,
        video_url: str,
        text: str,
        topic: str | None = None,
        *,
        poll_interval: float = 15.0,
        max_polls: int = 40,
    ) -> str:
        """Create, wait for, and publish a video post; returns the media id. Blocking — run it in a thread.

        Meta suggests about 30 s before the first check; a reel-length video is
        usually done within a few minutes, and a container left 24 h expires.
        """
        container_id = self.create_video_container(video_url, text, topic)
        logger.info("Created Threads video container %s", container_id)
        time.sleep(poll_interval)
        for i in range(max_polls):
            status, error = self.container_status(container_id)
            if status == "FINISHED":
                return self.publish_container(container_id)
            if status in ("ERROR", "EXPIRED"):
                raise RuntimeError(
                    f"Threads container {container_id} {status.lower()}"
                    + (f": {error}" if error else " (Meta gave no reason)")
                )
            if i % 4 == 0:
                logger.info("Waiting for Threads container %s (status: %s)", container_id, status)
            time.sleep(poll_interval)
        raise TimeoutError(f"Threads container {container_id} not ready after {max_polls * poll_interval:.0f}s")

    def post_comment(self, media_id: str, message: str) -> str:
        """Reply to our own post — the first comment. Text replies publish in one call."""
        body = self._post(
            "me/threads",
            {"media_type": "TEXT", "text": message, "reply_to_id": media_id, "auto_publish_text": "true"},
        )
        return str(body.get("id") or "")

    def get_permalink(self, media_id: str) -> str | None:
        return self._get(media_id, {"fields": "permalink"}).get("permalink")

    def delete_post(self, media_id: str) -> None:
        self._delete(media_id)

    # -- reading ----------------------------------------------------------

    def list_posts(self, max_pages: int = 20) -> list[dict[str, Any]]:
        """Every post on the profile, newest first, deduplicated across page seams."""
        posts: list[dict[str, Any]] = []
        seen: set[str] = set()
        params: dict[str, Any] | None = {"fields": _POST_FIELDS, "limit": 100}
        url: str | None = f"{GRAPH_BASE}/me/threads"
        for _ in range(max_pages):
            if not url:
                break
            started = time.time()
            resp = requests.get(url, params=params, headers=self._headers, timeout=_TIMEOUT)
            body = _checked(resp, started, "list posts")
            for post in body.get("data") or []:
                pid = str(post.get("id") or "")
                if pid and pid not in seen:
                    seen.add(pid)
                    posts.append(post)
            # The next link carries every parameter already.
            url, params = (body.get("paging") or {}).get("next"), None
        return posts

    def post_insights(self, media_id: str) -> dict[str, Any]:
        return self._get(f"{media_id}/insights", {"metric": ",".join(POST_METRICS)})

    def account_insights(self, metrics: str, since: int | None = None, until: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"metric": metrics}
        if since is not None:
            params["since"] = since
        if until is not None:
            params["until"] = until
        return self._get("me/threads_insights", params)

    def follower_demographics(self, breakdown: str) -> dict[str, Any]:
        """Needs 100+ followers; Threads answers an error below that."""
        return self._get("me/threads_insights", {"metric": "follower_demographics", "breakdown": breakdown})


class ThreadsServiceManager:
    """The client for a channel, from the token on its doc. Not cached: a refresh rewrites the token."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def get_service(self, channel_id: str) -> ThreadsService | None:
        channel = await self._db.channels.find_one({"channel_id": channel_id}, {"threads_tokens": 1})
        token = ((channel or {}).get("threads_tokens") or {}).get("access_token")
        if not token:
            logger.warning("No Threads token stored for channel '%s'", channel_id)
            return None
        return ThreadsService(token)
