"""Kind → adapter lookup.

The single place that knows which adapters exist. Registering a new app here is
the last step of adding one; nothing else in the codebase names a kind.
"""

from __future__ import annotations

from app.models.video_source import SourceKind, VideoSource
from app.services.video_sources.base import SourceAdapter
from app.services.video_sources.georank import GeoRankAdapter
from app.services.video_sources.vidforge import VidForgeAdapter

# Adapters hold no per-source state, so one instance each is enough.
_ADAPTERS: dict[str, SourceAdapter] = {
    a.kind: a
    for a in (
        GeoRankAdapter(),
        VidForgeAdapter(),
    )
}


def adapter_for_kind(kind: str) -> SourceAdapter:
    adapter = _ADAPTERS.get(kind)
    if adapter is None:
        raise ValueError(f"No adapter registered for source kind '{kind}' (known: {', '.join(sorted(_ADAPTERS))})")
    return adapter


def adapter_for(source: VideoSource) -> SourceAdapter:
    return adapter_for_kind(source.kind)


def known_kinds() -> list[SourceKind]:
    # Sorted so the UI's kind list does not reshuffle between restarts.
    return sorted(_ADAPTERS)  # type: ignore[return-value]
