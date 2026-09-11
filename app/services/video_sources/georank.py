"""Adapter for apps exposing the read-only export feed contract.

    GET  {list_path}?limit=&cursor=  -> {videos, nextCursor, urlTtlSeconds}
    GET  {detail_path}               -> one video with a fresh downloadUrl
    POST {mark_imported_path}        -> {externalVideoId}

Cursor-paginated, one static shared secret, and it both reports and accepts
delivery state — so a video the app already pushed to us is detectable.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.logger import get_logger
from app.models.video_source import GeoRankConfig, SourceVideo, VideoSource
from app.services.video_sources.base import (
    REQUEST_TIMEOUT_S,
    SourceAdapter,
    SourcePage,
    describe_http_error,
    mask_secret,
)

logger = get_logger(__name__)

# The callback is idempotent, so retrying a transient failure is free. A
# permanently missed callback risks a duplicate later, which is worth a few tries.
NOTIFY_ATTEMPTS = 3
NOTIFY_BACKOFF_S = 2.0


# The generation contract, read off the app itself
# (gerorank-visualizer-2: routes/consumerVideos.ts, lib/autoVideoPolicy.ts,
# middleware/serviceAuth.ts). Kept here so enabling it is a config value rather
# than folklore:
#
#   POST /api/videos  {"auto": true}   -> 201 {videoId, status: "rendering", ...}
#   GET  /api/videos/{id}/status       -> {status: "ready"|"rendering"|"failed"}
#
# `auto: true` is the "you choose" mode: the app fixes aspect and length to its
# house format, rotates voice and channel preset, drafts several ideas and keeps
# the highest-scoring one (skipping anything used in the last 30 days). The flag
# is explicit on purpose — without it the endpoint answers 400 "prompt is
# required" rather than quietly inventing a video.
#
# Note the vocabularies differ by design: a finished render is "ready" here and
# "completed" in the pull feed, which is why both are per-source config.
GEORANK_AUTO_GENERATION: dict[str, Any] = {
    "create_path": "/api/videos",
    "create_body": {"auto": True},
    "status_path": "/api/videos/{id}/status",
    "job_id_field": "videoId",
    "status_field": "status",
    "completed_status": "ready",
    "failed_statuses": ["failed"],
}


class GeoRankAdapter(SourceAdapter):
    kind = "georank"
    pushes_to_us = True

    @staticmethod
    def _cfg(source: VideoSource) -> GeoRankConfig:
        cfg = source.config
        assert isinstance(cfg, GeoRankConfig)  # guaranteed by the discriminated union
        return cfg

    def _headers(self, source: VideoSource) -> dict[str, str]:
        cfg = self._cfg(source)
        if cfg.auth_style == "api_key_header":
            return {"X-Api-Key": cfg.api_key}
        return {"Authorization": f"Bearer {cfg.api_key}"}

    def credential_hint(self, source: VideoSource) -> str:
        cfg = self._cfg(source)
        label = "Bearer" if cfg.auth_style == "bearer" else "X-Api-Key"
        return f"{label} {mask_secret(cfg.api_key)}"

    def supports_mark_imported(self, source: VideoSource) -> bool:
        return bool(self._cfg(source).mark_imported_path)

    async def authed_request(
        self,
        source: VideoSource,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        # X-Api-Key unconditionally, whatever auth_style the feed is configured
        # with. The read-only feed accepts either header, but the create endpoint
        # sits behind a different gate that reads x-api-key and ONLY x-api-key —
        # it dropped its Bearer fallback because an operator's session token is
        # also a bearer token. A bearer-configured source would otherwise list
        # videos happily and 401 the moment it asked for one.
        cfg = self._cfg(source)
        headers = {
            **self._headers(source),
            "X-Api-Key": cfg.api_key,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            resp = await client.request(method, f"{source.base_url}{path}", json=json_body, headers=headers)
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------

    @staticmethod
    def normalise(raw: dict[str, Any]) -> SourceVideo:
        """Map one feed video object onto our snake_case model."""
        return SourceVideo(
            id=str(raw["id"]),
            title=raw.get("title") or "Untitled",
            status=raw.get("status") or "unknown",
            duration_ms=raw.get("durationMs"),
            created_at=raw.get("createdAt"),
            thumbnail_url=raw.get("thumbnailUrl"),
            content_type=raw.get("contentType") or "video/mp4",
            already_sent_to_channel=bool(raw.get("alreadySentToChannel", False)),
            external_video_id=raw.get("externalVideoId"),
        )

    async def fetch_page(self, source: VideoSource, limit: int, cursor: str | None) -> SourcePage:
        cfg = self._cfg(source)
        params: dict[str, Any] = {"limit": min(limit, cfg.page_limit)}
        if cursor:
            params["cursor"] = cursor

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            resp = await client.get(
                f"{source.base_url}{cfg.list_path}",
                params=params,
                headers=self._headers(source),
            )
            resp.raise_for_status()
            data = resp.json()

        return SourcePage(
            videos=[self.normalise(v) for v in data.get("videos", []) if v.get("id")],
            next_cursor=data.get("nextCursor"),
            url_ttl_seconds=data.get("urlTtlSeconds"),
        )

    async def fetch_download_url(self, source: VideoSource, video_id: str) -> str:
        cfg = self._cfg(source)
        path = cfg.detail_path.replace("{id}", video_id)

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            resp = await client.get(f"{source.base_url}{path}", headers=self._headers(source))
            resp.raise_for_status()
            data = resp.json()

        # The detail endpoint may return the video bare or wrapped; accept both so
        # a harmless shape difference between deployments is not a hard failure.
        video = data.get("video", data)
        url = video.get("downloadUrl")
        if not url:
            raise ValueError(f"App returned no downloadUrl for video '{video_id}'")
        return str(url)

    async def mark_imported(self, source: VideoSource, video_id: str, our_video_id: str | None) -> str | None:
        cfg = self._cfg(source)
        if not cfg.mark_imported_path:
            return None

        url = f"{source.base_url}{cfg.mark_imported_path.replace('{id}', video_id)}"
        headers = {**self._headers(source), "Content-Type": "application/json"}
        # The body is optional in the contract, so a hand-marked video sends none
        # rather than claiming a video id that does not exist.
        payload = {"externalVideoId": our_video_id} if our_video_id else {}

        last_error = "unknown error"
        for attempt in range(1, NOTIFY_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code < 300:
                    return None
                # 404 means the app does not know this video and 401 means our key
                # is wrong; neither improves by trying again.
                if resp.status_code in (401, 403, 404):
                    return f"HTTP {resp.status_code} — {resp.text[:150].strip()}"
                last_error = f"HTTP {resp.status_code} — {resp.text[:150].strip()}"
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                last_error = describe_http_error(exc)

            if attempt < NOTIFY_ATTEMPTS:
                await asyncio.sleep(NOTIFY_BACKOFF_S * attempt)

        return f"{last_error} (after {NOTIFY_ATTEMPTS} attempts)"
