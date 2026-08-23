"""The contract every content-app adapter implements.

One adapter per app kind. Everything app-specific — auth, pagination, field
names, how delivery is marked — lives behind this interface, so the importer,
the worker and the UI never branch on kind.

Adding an app means adding a config model and an adapter. It must not mean
editing anything that an existing channel already depends on.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.models.video_source import SourceKind, SourceVideo, VideoSource

REQUEST_TIMEOUT_S = 30.0


@dataclass(slots=True)
class SourcePage:
    """One page of normalised videos, however the app paginated it.

    ``next_cursor`` is opaque to callers: a real cursor for one app, a stringified
    page number for another. Whoever produced it is the only one who reads it.
    """

    videos: list[SourceVideo] = field(default_factory=list)
    next_cursor: str | None = None
    url_ttl_seconds: int | None = None


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
