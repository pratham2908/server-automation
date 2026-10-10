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
from app.services.instagram_tokens import (
    PROVIDER_FACEBOOK,
    PROVIDER_INSTAGRAM,
    ig_user_id_for,
    provider_of,
    select_slot,
)

# Meta answers 500 "Please reduce the amount of data you're asking for" when a
# business_discovery page asks for too much at once. Halving the page is the
# remedy; below this floor the request is not the problem and retrying only
# burns calls against a shared rate budget.
MIN_MEDIA_PAGE_SIZE = 6


def graph_error_message(exc: Exception) -> str:
    """Meta's own words for a failed Graph call.

    ``requests`` raises "500 Server Error: Internal Server Error for url: ...",
    which says nothing about what Meta actually objected to — that lives in the
    response body. Callers surfacing an error to a human or another service want
    the body, not the status line.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    try:
        error = response.json().get("error") or {}
    except Exception:
        # A non-JSON body (an HTML error page, a proxy timeout) leaves nothing
        # better than the status line.
        return str(exc)

    message = error.get("message")
    if not message:
        return str(exc)

    codes: list[str] = []
    if error.get("code") is not None:
        codes.append(f"code {error['code']}")
    if error.get("error_subcode") is not None:
        codes.append(f"subcode {error['error_subcode']}")
    return f"{message} ({', '.join(codes)})" if codes else str(message)


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


def find_business_account_id(access_token: str, usernames: set[str]) -> str | None:
    """The Instagram business-account id a Facebook token reaches for one of *usernames*.

    A Facebook token addresses an account by its business id, which differs from
    the Instagram-scoped id an Instagram Login channel stores. Which route finds
    it depends on what kind of token was generated:

    * a **user** token lists its Pages at ``/me/accounts``; each Page's linked
      account is matched by username;
    * a **Page** token has no ``accounts`` edge, because ``/me`` *is* the Page, so
      the linked account is read straight off it.

    Either way the username must match, so a token for the wrong account is
    refused rather than attached.
    """
    wanted = {u.lstrip("@").strip().lower() for u in usernames if u}
    headers = {"Authorization": f"Bearer {access_token.strip()}"}
    fields = "instagram_business_account{id,username}"

    def matches(account: dict[str, Any]) -> bool:
        return str(account.get("username", "")).lower() in wanted

    resp = requests.get(
        f"{_FB_GRAPH_BASE}/me/accounts", params={"fields": fields, "limit": "100"}, headers=headers, timeout=30
    )
    if resp.ok:
        for page in resp.json().get("data", []):
            account = page.get("instagram_business_account") or {}
            if matches(account):
                return cast(str, account["id"])
        return None

    # Graph reports a Page token's missing edge as code 100 "nonexisting field (accounts)".
    # Anything else (expired token, no permission) is a real failure and must surface.
    if "nonexisting field (accounts)" not in graph_error_message(requests.HTTPError(response=resp)):
        resp.raise_for_status()
    page_resp = requests.get(f"{_FB_GRAPH_BASE}/me", params={"fields": fields}, headers=headers, timeout=30)
    page_resp.raise_for_status()
    account = page_resp.json().get("instagram_business_account") or {}
    return cast(str, account["id"]) if matches(account) else None


class InstagramService:
    """Wraps the Instagram Graph API for a single channel."""

    def __init__(
        self,
        access_token: str,
        *,
        provider: str = PROVIDER_FACEBOOK,
        db: Any = None,
        channel_id: str | None = None,
        token_field: str = "instagram_tokens",
    ) -> None:
        # Strip whitespace/newlines — a pasted token with a trailing "\n" produces
        # an "Invalid header value" (Bearer <token>\n) and fails every Graph call.
        self._token = access_token.strip()
        self._provider = provider
        self._base = _base_for(provider)
        self._db = db
        self._channel_id = channel_id
        # Which slot on the channel doc holds this token, so a refresh writes back to the right one.
        self._token_field = token_field

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

    def _user_node(self, ig_user_id: str) -> str:
        """Path segment for user-node endpoints (account / media / publish).

        Instagram-Login tokens are scoped to their own account, whose id lives in
        a different namespace than the stored Facebook business id — so address it
        as ``me``. Facebook Login keeps using the passed IG business id.
        """
        return "me" if self._provider == PROVIDER_INSTAGRAM else ig_user_id

    def get_account_info(self, ig_user_id: str) -> dict[str, Any]:
        """Fetch Instagram Business/Creator account metadata."""
        fields = "id,username,name,profile_picture_url,followers_count,media_count,biography"
        data = self._get(self._user_node(ig_user_id), {"fields": fields})
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

    def _discover_media_page(
        self, own_ig_user_id: str, target_username: str, page_size: int, after: str | None
    ) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
        """One page of business_discovery media, shrinking the ask if Meta balks.

        A 500 here means "Please reduce the amount of data you're asking for" —
        the page was too big, not wrong. So the same page is retried at half the
        size (50 → 25 → 12 → 6) before giving up, rather than failing a request
        that would have worked slightly smaller.

        Returns ``(items, paging, page_size_used)``; the size is handed back so
        the caller can keep the smaller page for the rest of the run instead of
        rediscovering the ceiling on every page.
        """
        size = page_size
        while True:
            cursor = f".after({after})" if after else ""
            fields = (
                f"business_discovery.username({target_username})"
                f"{{media.limit({size}){cursor}"
                f"{{id,caption,media_type,media_product_type,media_url,thumbnail_url,"
                f"permalink,timestamp,like_count,comments_count,"
                f"children{{id,media_type,media_url,thumbnail_url}}}}}}"
            )
            try:
                data = self._get(own_ig_user_id, {"fields": fields})
            except requests.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 500 and size > MIN_MEDIA_PAGE_SIZE:
                    smaller = max(MIN_MEDIA_PAGE_SIZE, size // 2)
                    logger.warning(
                        "business_discovery page of %d was too large for %s (%s) — retrying at %d",
                        size,
                        target_username,
                        graph_error_message(exc),
                        smaller,
                    )
                    size = smaller
                    continue
                raise

            media = data.get("business_discovery", {}).get("media", {})
            return list(media.get("data", [])), dict(media.get("paging") or {}), size

    def discover_all_media(
        self, own_ig_user_id: str, target_username: str, limit: int = 250, page_size: int = 50
    ) -> list[dict[str, Any]]:
        """Every recent media item on *target_username*, raw, with no filtering.

        Distinct from :meth:`discover_competitor_media`, which keeps only reels
        and shapes rows for the topic-discovery tables. This one is for callers
        that want the account's actual output — for a photo-led account, reels
        are a rounding error and dropping carousels loses almost everything.

        Paged rather than fetched in one shot, because asking for ``children``
        (the individual frames of a carousel) trips a complexity budget: Meta
        answers "Please reduce the amount of data you're asking for" as a 500.
        The threshold is not a fixed item count and moves with how much each
        post carries — 60 has been served where 140 was refused — so it is
        discovered by halving rather than hard-coded. A bare query tolerates a
        much larger limit, but then every carousel comes back as a single cover
        image, useless to a caller whose job is to download the post.

        Videos still carry no ``media_url`` at any page size; only
        ``thumbnail_url`` and the permalink. That is Instagram's rule for other
        people's media, not a consequence of paging.
        """
        self._require_business_discovery()

        # Never ask for more in one page than the caller wants in total: a
        # limit of 3 should cost one small page, not a 50-item page we discard
        # most of — and an oversized page is exactly what provokes the 500.
        size = max(1, min(page_size, limit))

        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        after: str | None = None

        # A guard against a cursor that never terminates, not a call budget: the
        # normal stop is Meta running out of pages. Keyed off the floor rather
        # than the starting size so a run that has to shrink mid-way can still
        # reach the caller's limit.
        max_pages = max(1, -(-limit // MIN_MEDIA_PAGE_SIZE)) + 2

        for _ in range(max_pages):
            page, paging, size = self._discover_media_page(own_ig_user_id, target_username, size, after)
            if not page:
                break

            for item in page:
                # Posting during pagination shifts every item down a slot, so one
                # can arrive on two consecutive pages — the same seam that once
                # imported a reel twice.
                media_id = item.get("id", "")
                if media_id and media_id not in seen:
                    seen.add(media_id)
                    collected.append(item)

            if len(collected) >= limit:
                break

            after = paging.get("cursors", {}).get("after")
            if not after:
                break

        return collected[:limit]

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
        # /media is newest-first and cursor-paged, so posting during pagination
        # shifts everything down a slot and an item can come back on two
        # consecutive pages. Keyed by id, first sighting wins — the same seam
        # that once imported a YouTube video twice.
        seen: set[str] = set()
        url: str | None = f"{self._user_node(ig_user_id)}/media"
        params: dict = {"fields": fields, "limit": "100"}

        while url:
            body = self._get(url, params=params)
            for item in body.get("data", []):
                if item.get("media_type") in ("VIDEO", "REEL") and item.get("id") not in seen:
                    seen.add(item["id"])
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

    _THREAD_FIELDS = "id,text,timestamp,like_count,username,replies.limit(50){id,text,timestamp,like_count,username}"

    @staticmethod
    def _thread_message(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "comment_id": item.get("id", ""),
            "text": item.get("text", ""),
            "like_count": int(item.get("like_count", 0)),
            "author": item.get("username", ""),
            "published_at": item.get("timestamp", ""),
        }

    def _thread_from_node(self, item: dict[str, Any], media_id: str) -> dict[str, Any]:
        replies = (item.get("replies") or {}).get("data", [])
        return {
            "top": self._thread_message(item),
            "replies": [self._thread_message(r) for r in replies],
            "comment_url": f"https://www.instagram.com/reels/{media_id}/",
        }

    def get_media_threads(self, media_id: str, max_threads: int = 50) -> list[dict[str, Any]]:
        """Top-level comments on a media item, each with its replies nested, newest first.

        One request returns the replies inline (``replies.limit(50){...}``), so reading a whole
        comment section costs one call per page rather than one per thread.
        """
        threads: list[dict[str, Any]] = []
        url: str | None = f"{media_id}/comments"
        params: dict[str, str] = {"fields": self._THREAD_FIELDS, "limit": str(min(50, max_threads))}
        while url and len(threads) < max_threads:
            body = self._get(url, params=params)
            threads.extend(self._thread_from_node(item, media_id) for item in body.get("data", []))
            next_url = body.get("paging", {}).get("next")
            params = {}
            url = next_url.replace(f"{self._base}/", "") if next_url else None
        return threads[:max_threads]

    def get_thread(self, comment_id: str, media_id: str = "") -> dict[str, Any]:
        """One thread by its top-level comment id, with every reply Instagram returns."""
        return self._thread_from_node(self._get(comment_id, {"fields": self._THREAD_FIELDS}), media_id)

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

    def post_comment(self, media_id: str, message: str) -> str:
        """Post a top-level comment on a media item we own.

        Requires ``instagram_business_manage_comments`` (Instagram Login) or
        ``instagram_manage_comments`` (Facebook Login). Returns the new
        comment's id.

        Note for callers hoping to *pin* this: Instagram has no API for that.
        The IG Comment node supports only read, delete, and hide — pinning
        exists solely as a manual action in the app. Posting first is the
        closest thing available.
        """
        from typing import cast

        return cast(str, self._post(f"{media_id}/comments", {"message": message}).get("id", ""))

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

    def _delete(self, endpoint: str, params: dict | None = None) -> dict:
        import time

        from app.services.metrics import metrics_service

        headers = {"Authorization": f"Bearer {self._token}"}
        start_time = time.time()
        try:
            resp = requests.delete(
                f"{self._base}/{endpoint}",
                params=params,
                headers=headers,
                timeout=30,
            )
            duration = (time.time() - start_time) * 1000

            if not resp.ok:
                metrics_service.record_external_call("instagram", duration, False)
                try:
                    logger.error("Instagram API DELETE failed (%d): %s", resp.status_code, resp.json())
                except Exception:
                    logger.error("Instagram API DELETE failed (%d): %s", resp.status_code, resp.text)
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

    def delete_media(self, media_id: str) -> None:
        """Delete a published post/reel from Instagram.

        Uses ``DELETE /{ig-media-id}`` (Instagram Graph API), which needs the
        ``instagram_manage_contents`` permission. Meta only supports deletion on
        the Facebook-Login path; an Instagram-Login token cannot delete, so that
        case raises ``ValueError`` for the caller to report rather than failing
        opaquely at the API.
        """
        if self._provider == PROVIDER_INSTAGRAM:
            raise ValueError(
                "Instagram media deletion is only supported for channels connected via "
                "Facebook Login, not the Instagram Login path."
            )
        self._delete(media_id)
        logger.info("Deleted Instagram media %s", media_id)

    def create_reel_container(
        self,
        ig_user_id: str,
        caption: str,
        *,
        upload_type: str = "resumable",
        thumb_offset: int | None = None,
        cover_url: str | None = None,
    ) -> dict[str, str]:
        """Create a Reel media container for resumable upload.

        Returns ``{"container_id": "...", "upload_uri": "..."}``.
        """
        params: dict[str, Any] = {
            "media_type": "REELS",
            "upload_type": upload_type,
            "caption": caption,
        }
        # See publish_reel_from_url: cover_url supersedes thumb_offset.
        if cover_url:
            params["cover_url"] = cover_url
        elif thumb_offset is not None:
            params["thumb_offset"] = str(thumb_offset)

        data = self._post(f"{self._user_node(ig_user_id)}/media", params)
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
            f"{self._user_node(ig_user_id)}/media_publish",
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
        cover_url: str | None = None,
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
        # A supplied cover image wins over a timestamp into the video, and the
        # two are not combined: Instagram ignores thumb_offset when cover_url is
        # set, so sending both would only make the request lie about its intent.
        # The URL must be publicly fetchable by Meta's servers for the duration
        # of container processing.
        if cover_url:
            params["cover_url"] = cover_url
        elif thumb_offset is not None:
            params["thumb_offset"] = str(thumb_offset)

        data = self._post(f"{self._user_node(ig_user_id)}/media", params)
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
    # Publishing (image / carousel / story posts)
    #
    # Each call below creates or reads exactly one thing and returns at once —
    # no polling. The post publisher persists every container id it gets back
    # and checks status on its next tick, so a slow video or a restart resumes
    # instead of creating a second container.
    # ------------------------------------------------------------------

    def _create_container(self, ig_user_id: str, params: dict[str, str]) -> str:
        data = self._post(f"{self._user_node(ig_user_id)}/media", params)
        container_id = str(data.get("id") or "")
        if not container_id:
            raise RuntimeError(f"Instagram returned no container id: {data}")
        return container_id

    def create_image_container(
        self,
        ig_user_id: str,
        image_url: str,
        *,
        caption: str | None = None,
        is_carousel_item: bool = False,
        alt_text: str | None = None,
        location_id: str | None = None,
    ) -> str:
        """Container for a single image post, or one image slide of a carousel.

        ``media_type`` is omitted on purpose: that is how the API spells "image".
        A carousel item carries no caption — the API has no per-slide caption.
        """
        params: dict[str, str] = {"image_url": image_url}
        if is_carousel_item:
            params["is_carousel_item"] = "true"
        elif caption:
            params["caption"] = caption
        # A location belongs to the post, never to one slide of a carousel.
        if location_id and not is_carousel_item:
            params["location_id"] = location_id
        if alt_text:
            params["alt_text"] = alt_text
        return self._create_container(ig_user_id, params)

    def create_carousel_video_item(self, ig_user_id: str, video_url: str) -> str:
        """Container for one video slide of a carousel."""
        return self._create_container(
            ig_user_id,
            {"media_type": "VIDEO", "is_carousel_item": "true", "video_url": video_url},
        )

    def create_carousel_container(
        self, ig_user_id: str, children: list[str], caption: str, location_id: str | None = None
    ) -> str:
        """Parent container for a carousel; *children* are finished item containers, in order."""
        params: dict[str, str] = {"media_type": "CAROUSEL", "children": ",".join(children)}
        if caption:
            params["caption"] = caption
        if location_id:
            params["location_id"] = location_id
        return self._create_container(ig_user_id, params)

    def create_story_container(
        self,
        ig_user_id: str,
        *,
        image_url: str | None = None,
        video_url: str | None = None,
    ) -> str:
        """Story container from exactly one of an image or a video URL. Stories take no caption."""
        if bool(image_url) == bool(video_url):
            raise ValueError("A story needs exactly one of image_url or video_url")
        params: dict[str, str] = {"media_type": "STORIES"}
        if image_url:
            params["image_url"] = image_url
        else:
            params["video_url"] = str(video_url)
        return self._create_container(ig_user_id, params)

    def get_permalink(self, media_id: str) -> str | None:
        data = self._get(media_id, {"fields": "permalink"})
        permalink = data.get("permalink")
        return str(permalink) if permalink else None

    def get_media_page(self, ig_user_id: str, *, limit: int = 25, after: str | None = None) -> dict[str, Any]:
        """One newest-first page of the account's media, every type.

        Returns ``{"data": [...], "next_cursor": str | None}``. Unlike
        ``get_reels`` this does not walk every page: the feed view and the
        manual-post detector only ever want the most recent handful.
        """
        fields = (
            "id,caption,media_type,media_product_type,media_url,thumbnail_url,timestamp,permalink,"
            "like_count,comments_count,children{media_type,media_url,thumbnail_url}"
        )
        params: dict[str, str] = {"fields": fields, "limit": str(limit)}
        if after:
            params["after"] = after
        body = self._get(f"{self._user_node(ig_user_id)}/media", params)
        paging = body.get("paging") or {}
        # The API returns an "after" cursor even on the last page; only a "next"
        # link means there really is more.
        next_cursor = (paging.get("cursors") or {}).get("after") if paging.get("next") else None
        return {"data": list(body.get("data") or []), "next_cursor": next_cursor}

    def get_media_insights(self, media_id: str, metrics: list[str]) -> tuple[dict[str, int], list[str]]:
        """Lifetime insights for one media, and the metrics it could not report.

        The insights endpoint fails the whole request when any one metric does
        not apply to the media type (a carousel has no ``views`` on some API
        versions, for instance). So ask for everything once, and only on an
        error fall back to one metric at a time to find out which were refused.
        """
        try:
            return self._parse_insights(self._get(f"{media_id}/insights", {"metric": ",".join(metrics)})), []
        except requests.HTTPError as exc:
            logger.info("Insights for %s refused as a batch (%s) — asking per metric", media_id, exc)

        values: dict[str, int] = {}
        unavailable: list[str] = []
        for metric in metrics:
            try:
                values.update(self._parse_insights(self._get(f"{media_id}/insights", {"metric": metric})))
            except requests.HTTPError as exc:
                logger.info("Insight '%s' unavailable for %s: %s", metric, media_id, exc)
                unavailable.append(metric)
        return values, unavailable

    @staticmethod
    def _parse_insights(body: dict[str, Any]) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in body.get("data", []):
            name = entry.get("name")
            if not name:
                continue
            # Lifetime media metrics come as ``values[0].value``; newer API
            # versions report some as ``total_value.value`` instead.
            values = entry.get("values") or []
            raw = values[0].get("value", 0) if values else (entry.get("total_value") or {}).get("value", 0)
            out[str(name)] = int(raw or 0)
        return out

    def get_publishing_limit(self, ig_user_id: str) -> dict[str, int]:
        """The account's rolling API-publish quota: ``quota_total``, ``quota_usage``, ``quota_duration``."""
        body = self._get(
            f"{self._user_node(ig_user_id)}/content_publishing_limit",
            {"fields": "config,quota_usage"},
        )
        rows = body.get("data") or [{}]
        row = rows[0] if rows else {}
        config = row.get("config") or {}
        return {
            "quota_total": int(config.get("quota_total", 0)),
            "quota_usage": int(row.get("quota_usage", 0)),
            "quota_duration": int(config.get("quota_duration", 0)),
        }

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
                                f"{self._token_field}.access_token": new_token,
                                f"{self._token_field}.expires_at": expires_at,
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
        self._cache: dict[tuple[str, str], InstagramService] = {}

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

    async def get_service(self, channel_id: str, prefer: str | None = None) -> InstagramService | None:
        """The client for this channel, using the token of login type *prefer* when it has one."""
        resolved = await self.get_service_and_user_id(channel_id, prefer)
        return resolved[0] if resolved else None

    async def get_service_and_user_id(
        self, channel_id: str, prefer: str | None = None
    ) -> tuple[InstagramService, str] | None:
        """The client plus the Instagram user id that token addresses.

        The id matters: a Facebook token on a channel whose primary is Instagram
        Login needs the business-account id stored with *that* token, not the
        channel's Instagram-scoped one.
        """
        channel = await self._db.channels.find_one({"channel_id": channel_id})
        if not channel:
            logger.warning("No channel '%s'", channel_id)
            return None
        slot = select_slot(channel, prefer)
        if not slot:
            logger.warning("No Instagram tokens stored for channel '%s'", channel_id)
            return None
        field, tokens = slot
        ig_user_id = ig_user_id_for(channel, tokens)

        cache_key = (channel_id, field)
        cached = self._cache.get(cache_key)
        if cached:
            return cached, ig_user_id

        try:
            service = InstagramService(
                access_token=tokens["access_token"],
                provider=provider_of(tokens),
                db=self._db,
                channel_id=channel_id,
                token_field=field,
            )
            self._cache[cache_key] = service
            logger.info("Instagram service initialised for channel '%s' (%s token)", channel_id, provider_of(tokens))
            return service, ig_user_id
        except Exception:
            logger.exception("Failed to init Instagram service for channel '%s'", channel_id)
            return None

    def invalidate(self, channel_id: str) -> None:
        """Forget every cached client for the channel; the next call re-reads its tokens."""
        for key in [k for k in self._cache if k[0] == channel_id]:
            del self._cache[key]
