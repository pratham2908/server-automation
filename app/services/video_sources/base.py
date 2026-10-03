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


# What came back from asking an app for the one video to publish now.
#
#   ready       — a finished render is waiting; ``video_id`` says which.
#   generating  — nothing was ready, so the app started one. Poll again.
#   unavailable — transient on the app's side (it could not pick a topic). Retry.
#   unsupported — this app has no such endpoint; use the catalogue instead.
TodaysVideoState = Literal["ready", "generating", "unavailable", "unsupported"]


@dataclass(slots=True)
class TodaysVideo:
    """The app's answer to "what should we publish now?"."""

    state: TodaysVideoState
    # The app's own id for the render, set only when state is "ready". It is the
    # same id the catalogue lists, which is what lets an ordinary import take it.
    video_id: str | None = None
    # What the app is making, when it told us. Only ever for a log or an email.
    title: str | None = None
    # The app's own pacing hint for the next poll, honoured rather than guessed.
    retry_after_seconds: int | None = None
    reason: str | None = None
    # The app telling us something it wanted to do did not happen — see
    # ``is_noteworthy_remark``. Set only when it is worth a person's attention, so
    # anything here belongs in the daily email rather than a log line.
    remark: str | None = None
    # A "generating" app that took our callback offer and will call when done, so
    # the scheduler stops asking. False for an app that predates callbacks.
    callback_accepted: bool = False
    # Set by the service when the app accepted: which callback record it holds.
    callback_id: str | None = None


# The one thing an app tells us that is not a complaint: no format was scheduled
# for today, so a standard video is exactly right. Anything else it says — a
# scheduled format with no next episode to air, a render that went missing, or
# some case we have not seen yet — means something it meant to do did not happen.
#
# The match is against the BENIGN case on purpose. Listing the problems instead
# would silently swallow the next one the app learns to report, and an unfamiliar
# remark is precisely the kind worth reading. So: recognise "normal", surface the
# rest. A reworded benign message would over-report, which is the safe direction.
BENIGN_REMARK_PREFIX = "no format is scheduled"


def is_noteworthy_remark(remark: str | None) -> bool:
    """Whether an app's remark is worth putting in front of a person.

    Anchored at the START of the string rather than searched for anywhere in it,
    because the app builds this message as "which branch I took" followed by an
    optional "and here is what then failed". Only the opening clause says whether
    a scheduled format was missed, so only the opening clause can clear a remark
    as routine — a complaint that merely mentions scheduling further along still
    gets read out.
    """
    if not remark or not remark.strip():
        return False
    return not remark.strip().lower().startswith(BENIGN_REMARK_PREFIX)


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
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Call the app with whatever auth it uses, raising for a bad status.

        Generation below is written once against this hook, so an app that can
        render gains the capability from its config alone — no new adapter code
        beyond whatever authenticating it already required.
        """

    # ------------------------------------------------------------------
    # Today's video — optional capability: the app decides what to publish
    # ------------------------------------------------------------------

    def todays_video_path(self, source: VideoSource) -> str:
        """Where to ask this app for the one video to publish now; "" if it cannot.

        Base returns "" because this is the georank feed contract, not something
        every app speaks. An adapter whose app serves it overrides this and reads
        the path off its own config.
        """
        return ""

    def supports_todays_video(self, source: VideoSource) -> bool:
        return bool(self.todays_video_path(source))

    async def request_todays_video(
        self, source: VideoSource, callback_headers: dict[str, str] | None = None
    ) -> TodaysVideo:
        """Ask the app what to publish now; it returns one ready or starts one.

        This replaces two of our decisions with one of theirs. We used to scan the
        catalogue for anything unimported and, finding nothing, ask for a render —
        which meant we chose the video and they only chose the topic. The app knows
        its own schedule (which format belongs to which day, what it has pre-made,
        what is already in flight), so it is better placed to answer than we are.

        Safe to call repeatedly: the contract guarantees the app reports work
        already in flight rather than starting a second one, so a poll cannot fan
        out spend. That guarantee is what lets the scheduler treat this as both the
        request and the status check.

        ``callback_headers`` offer the app a one-time callback (see
        ``today_callbacks``); an app that takes it says ``callbackAccepted: true``.
        """
        path = self.todays_video_path(source)
        if not path:
            return TodaysVideo(state="unsupported", reason="this app has no today endpoint")

        try:
            # The same generous budget as a create call, and for the same reason:
            # when nothing is ready this request *starts* a video, drafting a topic
            # with grounded model calls inline before it answers. A listing-sized
            # 30s budget cut exactly that work off twice before.
            resp = await self.authed_request(
                source, "GET", path, timeout=GENERATION_CREATE_TIMEOUT_S, extra_headers=callback_headers
            )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (404, 405, 501):
                # Deployed without the endpoint. Not an error — the caller falls
                # back to the catalogue, which is how this app used to work.
                return TodaysVideo(state="unsupported", reason=f"HTTP {code} at {path}")
            if code == 503:
                # The app's documented "transient, retry shortly", and also what
                # its gate answers when the feed is unconfigured. Both want a retry
                # rather than a skipped slot.
                return TodaysVideo(state="unavailable", reason=describe_http_error(exc))
            raise

        try:
            data = resp.json()
        except ValueError:
            raise SourceUnavailableError(f"'{source.name}' answered the today call with no JSON") from None
        if not isinstance(data, dict):
            raise SourceUnavailableError(f"'{source.name}' answered with {type(data).__name__}, not an object")

        # The body's own status is authoritative; the HTTP code mirrors it.
        state = str(data.get("status") or "").strip().lower()

        # The app explains itself when it could not do the thing it meant to — it
        # served a standard video because today's scheduled format had no next
        # episode, say. Only the noteworthy ones travel; a plain open day is normal
        # and would be daily noise in the summary.
        said = data.get("reason")
        remark = str(said).strip()[:400] if said else None
        remark = remark if is_noteworthy_remark(remark) else None
        if state == "ready":
            video = data.get("video")
            video_id = str(video.get("id")) if isinstance(video, dict) and video.get("id") else ""
            if not video_id:
                raise SourceUnavailableError(f"'{source.name}' said ready but named no video")
            title = video.get("title") if isinstance(video, dict) else None
            return TodaysVideo(state="ready", video_id=video_id, title=str(title) if title else None, remark=remark)

        if state == "generating":
            retry = data.get("retryAfterSeconds")
            return TodaysVideo(
                state="generating",
                title=str(data["title"]) if data.get("title") else None,
                retry_after_seconds=int(retry) if isinstance(retry, int | float) and retry > 0 else None,
                remark=remark,
                # Strictly ``true``: a truthy string from a confused app must not
                # silence the polling it would still need.
                callback_accepted=data.get("callbackAccepted") is True,
            )

        if state == "unavailable":
            # Here the app folds its explanation into the failure text, so the same
            # string is both why we are retrying and, when noteworthy, the remark.
            return TodaysVideo(
                state="unavailable",
                reason=str(data.get("reason") or "the app is not ready")[:200],
                remark=remark,
            )

        raise SourceUnavailableError(f"'{source.name}' answered an unknown today status {state!r}")

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
