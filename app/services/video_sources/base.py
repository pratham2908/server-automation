"""The contract every content-app adapter implements.

One adapter per app kind. Everything app-specific — auth, pagination, field
names, how delivery is marked — lives behind this interface, so the importer,
the worker and the UI never branch on kind.

Adding an app means adding a config model and an adapter. It must not mean
editing anything that an existing channel already depends on.
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from app.models.video_source import GenerationConfig, SourceKind, SourceVideo, VideoSource

REQUEST_TIMEOUT_S = 30.0

# Asking for a render is not the same shape of call as fetching a page. GeoRank
# runs two sequential grounded LLM calls (a brief, then the ideas) *inside* the
# request before it answers, and 30s — a listing budget — cut that off twice in
# three nights, skipping the slot for work the app was still willing to do.
GENERATION_CREATE_TIMEOUT_S = 120.0

# Retries for the create call. Deliberately small: each attempt can itself take
# two minutes, and the scheduler only has the hour before the slot.
GENERATION_ATTEMPTS = 3
GENERATION_BACKOFF_S = 3.0


@dataclass(slots=True)
class SourcePage:
    """One page of normalised videos, however the app paginated it.

    ``next_cursor`` is opaque to callers: a real cursor for one app, a stringified
    page number for another. Whoever produced it is the only one who reads it.
    """

    videos: list[SourceVideo] = field(default_factory=list)
    next_cursor: str | None = None
    url_ttl_seconds: int | None = None


# Where one requested render has got to. "pending" also covers an app that
# cannot report job state at all — the catalogue is then the source of truth.
GenerationState = Literal["pending", "completed", "failed"]


class SourceUnavailableError(Exception):
    """The app could not be reached, or refused us. Upstream, not our caller."""


class SourceAdapter(abc.ABC):
    """Talks to one kind of content app."""

    # Always one of the config discriminator values — an adapter with no matching
    # config could never be reached, since a source's kind is what selects it.
    kind: SourceKind

    # Whether this app can also push videos to us through the external upload API.
    # It decides how badly a failed delivery callback matters: for an app that
    # pushes, a missed callback invites a duplicate we cannot dedup, because a push
    # carries no source_video_id. For an app we only ever pull from, our own
    # source_video_id dedup already covers it and the callback is bookkeeping.
    pushes_to_us: bool = False

    # What this app calls a bundle of related videos, if it bundles them at all.
    # Naming it here keeps the word out of the UI, which only knows that some
    # videos share a group and that the group has a noun.
    group_noun: str | None = None

    @abc.abstractmethod
    async def fetch_page(self, source: VideoSource, limit: int, cursor: str | None) -> SourcePage:
        """Return one page of finished videos, newest first."""

    @abc.abstractmethod
    async def fetch_download_url(self, source: VideoSource, video_id: str) -> str:
        """Return a freshly minted download URL for one video.

        Always called at transfer time rather than read from a listing: every app
        here hands out presigned URLs that expire, and a queued job can outlive one.
        """

    @abc.abstractmethod
    async def mark_imported(self, source: VideoSource, video_id: str, our_video_id: str | None) -> str | None:
        """Tell the app we have this video. Returns None on success, else why not.

        ``our_video_id`` is None when an operator marks a video by hand, which is
        how they retire something the channel already published through another
        route — there is no local video record to point at.

        Never raises: a failed callback must not destroy an otherwise-good import.
        Adapters for apps without the capability return None immediately.
        """

    @abc.abstractmethod
    def supports_mark_imported(self, source: VideoSource) -> bool:
        """Whether this source can be told about an ingest at all."""

    @abc.abstractmethod
    def credential_hint(self, source: VideoSource) -> str:
        """How this source authenticates, with the secret redacted, for display."""

    @abc.abstractmethod
    async def authed_request(
        self,
        source: VideoSource,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> httpx.Response:
        """Call the app with whatever auth it uses, raising for a bad status.

        Generation below is written once against this hook, so an app that can
        render gains the capability from its config alone — no new adapter code
        beyond whatever authenticating it already required.
        """

    # ------------------------------------------------------------------
    # Generation — optional capability, entirely config-driven
    # ------------------------------------------------------------------

    @staticmethod
    def generation_config(source: VideoSource) -> GenerationConfig | None:
        return source.config.generation

    def supports_generation(self, source: VideoSource) -> bool:
        cfg = self.generation_config(source)
        return bool(cfg and cfg.create_path)

    async def request_generation(self, source: VideoSource) -> str:
        """Ask the app for one new render; returns its job id, '' if it gives none.

        The body comes from the config rather than being fixed here, because "make
        something good, you choose" is spelled differently by every app — GeoRank
        wants an explicit ``{"auto": true}`` and answers a bodyless request with a
        400. What stays constant is that we never pick the subject: the app knows
        its own content pipeline far better than we do.
        """
        cfg = self.generation_config(source)
        if cfg is None or not cfg.create_path:
            raise SourceUnavailableError(f"Source '{source.name}' cannot generate videos")

        resp = await self._create_with_retry(source, cfg)
        try:
            data = resp.json()
        except ValueError:
            return ""  # accepted, but told us nothing pollable
        if not isinstance(data, dict):
            return ""
        # Accept the job bare or wrapped, as fetch_download_url does: a harmless
        # shape difference between deployments should not fail an accepted render.
        payload = data["job"] if isinstance(data.get("job"), dict) else data
        job_id = payload.get(cfg.job_id_field)
        return str(job_id) if job_id else ""

    async def _create_with_retry(self, source: VideoSource, cfg: GenerationConfig) -> httpx.Response:
        """POST the create call, retrying only when it is provably safe to.

        A retry here can cost a second video, so the rule is narrow: retry only
        when the app ANSWERED with a server error. An answer proves it did not
        start a render, so asking again cannot duplicate one — that covers the
        transient ``502 topic_unavailable`` its idea generation returns.

        A timeout or a transport failure is NOT retried, however tempting. We
        cannot tell "never arrived" from "arrived, started a render, and we
        stopped listening", and guessing wrong bills a render nobody watches.
        The slot is skipped instead and the next slot retries an hour later.

        A 4xx is never retried either: the request itself is wrong, and repeating
        it just annoys the app.
        """
        last: httpx.HTTPStatusError | None = None
        for attempt in range(1, GENERATION_ATTEMPTS + 1):
            try:
                return await self.authed_request(
                    source,
                    "POST",
                    cfg.create_path,
                    json_body=dict(cfg.create_body),
                    timeout=GENERATION_CREATE_TIMEOUT_S,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise
                last = exc
                if attempt < GENERATION_ATTEMPTS:
                    await asyncio.sleep(GENERATION_BACKOFF_S * attempt)
        assert last is not None  # only reachable after a 5xx set it
        raise last

    async def generation_state(self, source: VideoSource, job_id: str) -> GenerationState:
        """Where a render has got to.

        "pending" whenever the app cannot tell us — no status_path, no job id, or
        an unrecognised status. Completion is then detected by the video showing
        up in the catalogue instead, which every app supports by definition.
        """
        cfg = self.generation_config(source)
        if cfg is None or not cfg.status_path or not job_id:
            return "pending"

        resp = await self.authed_request(source, "GET", cfg.status_path.replace("{id}", job_id))
        try:
            data = resp.json()
        except ValueError:
            return "pending"
        if not isinstance(data, dict):
            return "pending"
        payload = data["job"] if isinstance(data.get("job"), dict) else data

        raw = str(payload.get(cfg.status_field) or "").strip().lower()
        if raw and raw == cfg.completed_status.strip().lower():
            return "completed"
        if raw in {v.strip().lower() for v in cfg.failed_statuses}:
            return "failed"
        return "pending"

    async def probe(self, source: VideoSource) -> dict[str, Any]:
        """Verify credentials by asking for a single video.

        The default works for any adapter whose ``fetch_page`` is cheap; override
        only when an app offers something better.
        """
        page = await self.fetch_page(source, limit=1, cursor=None)
        return {
            "ok": True,
            "base_url": source.base_url,
            "has_content": bool(page.videos),
            "url_ttl_seconds": page.url_ttl_seconds,
        }


def describe_http_error(exc: Exception) -> str:
    """Turn a transport or status failure into something worth reading in the UI."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return f"HTTP {code} — the app rejected our credentials"
        if code == 404:
            return f"HTTP {code} — endpoint not found (check the configured path)"
        body = exc.response.text[:200].strip()
        return f"HTTP {code}{f' — {body}' if body else ''}"
    if isinstance(exc, httpx.TimeoutException):
        return f"Timed out after {REQUEST_TIMEOUT_S:.0f}s"
    if isinstance(exc, httpx.RequestError):
        return f"Could not reach the app: {exc}"
    if isinstance(exc, SourceUnavailableError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def mask_secret(secret: str) -> str:
    """Show only enough of a secret to tell two of them apart."""
    if len(secret) <= 4:
        return "****"
    return f"****{secret[-4:]}"
