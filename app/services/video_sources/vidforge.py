"""Adapter for VidForge studio libraries.

    POST  {login_path}   {email, password, app}  -> {accessToken, refreshToken, profile}
    GET   {list_path}?kind=&status=&page=&limit= -> {videos, pagination}
    GET   {detail_path}                          -> one video, including downloadUrl
    PATCH {detail_path}  {sent_flag: true}       -> mark delivered

Differs from the feed contract in every dimension that matters: page numbers
instead of cursors, ``_id``/``name``/``duration``-in-seconds instead of
``id``/``title``/``durationMs``, and an account login instead of a static key.

The ``app`` key decides which profile's library is visible; logging in with a
different key returns a different, isolated library.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

from app.logger import get_logger
from app.models.video_source import SourceVideo, VideoSource, VidForgeConfig
from app.services.video_sources.base import (
    REQUEST_TIMEOUT_S,
    SourceAdapter,
    SourcePage,
    SourceUnavailableError,
    describe_http_error,
)

logger = get_logger(__name__)

# Access tokens last 15 minutes. Refresh tokens rotate, and a rotated token that
# two processes both hold invalidates itself — so we skip refresh entirely and
# just log in again. We hold the password regardless, and a login every quarter
# hour of actual use is cheaper than getting rotation wrong.
TOKEN_TTL_S = 15 * 60
TOKEN_SAFETY_MARGIN_S = 120

_tokens: dict[str, tuple[str, float]] = {}
_token_locks: dict[str, asyncio.Lock] = {}


def _cache_key(source: VideoSource) -> str:
    """Identifies the *account*, not just the source.

    Keying on source_id alone would let a credential change keep serving the
    previous account's token until it expired — reading one profile's library
    while the config says another. Including the account makes the change take
    effect on the next call.
    """
    cfg = source.config
    assert isinstance(cfg, VidForgeConfig)
    return f"{source.source_id}|{cfg.email}|{cfg.app_key}"


def _lock_for(key: str) -> asyncio.Lock:
    """One lock per account so concurrent calls single-flight their login."""
    lock = _token_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _token_locks[key] = lock
    return lock


def forget_token(key: str) -> None:
    """Drop a cached token, e.g. after the app rejects it."""
    _tokens.pop(key, None)


# An export is named after the clip it came from, with that clip's id appended:
# "Why Birds Don't Fry on Power Lines (6a85f6aec7ac28f0e2efbd12)". The id makes
# every take of one episode read as a different title, so it comes off the label.
_RENDER_SUFFIX = re.compile(r"\s*\([0-9a-f]{24}\)\s*$")


def episode_label(name: str) -> str:
    """The episode's title, as carried by any one of its exports."""
    return _RENDER_SUFFIX.sub("", name).strip() or name


class VidForgeAdapter(SourceAdapter):
    kind = "vidforge"
    # Pull-only: the studio has no route into our external upload API.
    pushes_to_us = False
    # Exports descend from an episode, and an episode is usually rendered several
    # times before one is chosen — so the takes belong together.
    group_noun = "Episode"

    @staticmethod
    def _cfg(source: VideoSource) -> VidForgeConfig:
        cfg = source.config
        assert isinstance(cfg, VidForgeConfig)  # guaranteed by the discriminated union
        return cfg

    def credential_hint(self, source: VideoSource) -> str:
        cfg = self._cfg(source)
        return f"login {cfg.email} (app: {cfg.app_key})"

    def supports_mark_imported(self, source: VideoSource) -> bool:
        return bool(self._cfg(source).mark_imported_path)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def _login(self, source: VideoSource) -> str:
        cfg = self._cfg(source)
        payload = {"email": cfg.email, "password": cfg.password, "app": cfg.app_key}
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            resp = await client.post(f"{source.base_url}{cfg.login_path}", json=payload)
            if resp.status_code == 400:
                # An unrecognised app key is a 400 and is never silently defaulted,
                # so say so rather than reporting a generic bad request.
                raise SourceUnavailableError(
                    f"Login rejected (HTTP 400) — check the app key '{cfg.app_key}' and the credentials"
                )
            resp.raise_for_status()
            data = resp.json()

        token = data.get("accessToken")
        if not token:
            raise SourceUnavailableError("Login succeeded but returned no accessToken")
        logger.info(
            "VidForge login ok for source %s — profile '%s' (app: %s)",
            source.source_id,
            (data.get("profile") or {}).get("name"),
            data.get("app"),
        )
        return str(token)

    async def _token(self, source: VideoSource, *, force: bool = False) -> str:
        """A valid access token, logging in only when the cached one is stale."""
        key = _cache_key(source)
        if not force:
            cached = _tokens.get(key)
            if cached and cached[1] > time.monotonic():
                return cached[0]

        async with _lock_for(key):
            # Another caller may have refreshed while we waited for the lock.
            cached = _tokens.get(key)
            if not force and cached and cached[1] > time.monotonic():
                return cached[0]
            token = await self._login(source)
            _tokens[key] = (token, time.monotonic() + TOKEN_TTL_S - TOKEN_SAFETY_MARGIN_S)
            return token

    async def _request(
        self,
        source: VideoSource,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Call the app, re-logging in once if the token turns out to be dead."""
        for force in (False, True):
            token = await self._token(source, force=force)
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
                resp = await client.request(
                    method,
                    f"{source.base_url}{path}",
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
            if resp.status_code != 401:
                resp.raise_for_status()
                return resp
            forget_token(_cache_key(source))
        raise SourceUnavailableError("The app rejected our credentials twice — check the email and password")

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    @staticmethod
    def normalise(raw: dict[str, Any], sent_flag_field: str) -> SourceVideo:
        """Map one VidForge video onto our model.

        ``duration`` is seconds here and milliseconds in the feed contract, so it
        is converted rather than passed through — otherwise every clip renders 0:00.
        """
        duration_s = raw.get("duration")
        name = raw.get("name") or "Untitled"
        episode_id = raw.get("sourceEpisodeId")
        return SourceVideo(
            id=str(raw["_id"]),
            title=name,
            status=raw.get("status") or "unknown",
            duration_ms=int(duration_s * 1000) if isinstance(duration_s, (int, float)) else None,
            created_at=raw.get("createdAt"),
            thumbnail_url=raw.get("thumbnailUrl"),
            content_type="video/mp4",
            size_bytes=raw.get("fileSizeBytes"),
            already_sent_to_channel=bool(raw.get(sent_flag_field) or False),
            # VidForge stores no link back to the video it created here, so a push
            # it performed cannot be detected the way the feed contract allows.
            external_video_id=None,
            group_id=str(episode_id) if episode_id else None,
            # Derived per video rather than from the set, so every take of an
            # episode arrives with the same label even when they land on
            # different pages.
            group_label=episode_label(name) if episode_id else None,
        )

    async def fetch_page(self, source: VideoSource, limit: int, cursor: str | None) -> SourcePage:
        cfg = self._cfg(source)
        page = int(cursor) if cursor and cursor.isdigit() else 1

        params: dict[str, Any] = {"page": page, "limit": min(limit, cfg.page_limit)}
        if cfg.video_kind:
            params["kind"] = cfg.video_kind
        if cfg.status:
            params["status"] = cfg.status

        resp = await self._request(source, "GET", cfg.list_path, params=params)
        data = resp.json()

        pagination = data.get("pagination") or {}
        # The guide documents this key as `pages`, the deployment returns
        # `totalPages`; accept either rather than silently paginating once.
        total_pages = pagination.get("totalPages", pagination.get("pages", 1)) or 1
        current = pagination.get("page", page) or page
        next_cursor = str(current + 1) if current < total_pages else None

        return SourcePage(
            videos=[self.normalise(v, cfg.sent_flag_field) for v in data.get("videos", []) if v.get("_id")],
            next_cursor=next_cursor,
            url_ttl_seconds=None,
        )

    async def fetch_download_url(self, source: VideoSource, video_id: str) -> str:
        cfg = self._cfg(source)
        resp = await self._request(source, "GET", cfg.detail_path.replace("{id}", video_id))
        data = resp.json()
        url = data.get("downloadUrl") or data.get("videoUrl")
        if not url:
            raise ValueError(f"App returned no downloadUrl for video '{video_id}'")
        return str(url)

    # ------------------------------------------------------------------
    # Marking
    # ------------------------------------------------------------------

    async def mark_imported(self, source: VideoSource, video_id: str, our_video_id: str | None) -> str | None:
        """PATCH the delivery flag, then read it back to prove it stuck.

        This endpoint accepts unknown fields and answers 200 for them, so a
        deployment without the flag reports success while changing nothing. Left
        unchecked that silently re-imports the same videos forever, so the write
        is verified rather than trusted.
        """
        cfg = self._cfg(source)
        if not cfg.mark_imported_path:
            return None

        path = cfg.mark_imported_path.replace("{id}", video_id)
        try:
            await self._request(source, "PATCH", path, json_body={cfg.sent_flag_field: True})
        except Exception as exc:
            return describe_http_error(exc)

        try:
            check = await self._request(source, "GET", cfg.detail_path.replace("{id}", video_id))
            body = check.json()
        except Exception as exc:
            return f"marked, but could not verify it: {describe_http_error(exc)}"

        if cfg.sent_flag_field not in body:
            return (
                f"the app accepted the write but does not implement '{cfg.sent_flag_field}' — "
                "it will keep offering this video as unsent"
            )
        if not body.get(cfg.sent_flag_field):
            return f"the app accepted the write but '{cfg.sent_flag_field}' is still {body.get(cfg.sent_flag_field)!r}"
        return None
