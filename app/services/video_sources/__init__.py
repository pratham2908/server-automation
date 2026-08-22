"""Per-kind adapters for the content apps channels pull their videos from."""

from app.services.video_sources.base import (
    SourceAdapter,
    SourcePage,
    SourceUnavailableError,
    describe_http_error,
    mask_secret,
)
from app.services.video_sources.registry import adapter_for, adapter_for_kind, known_kinds

__all__ = [
    "SourceAdapter",
    "SourcePage",
    "SourceUnavailableError",
    "adapter_for",
    "adapter_for_kind",
    "describe_http_error",
    "known_kinds",
    "mask_secret",
]
