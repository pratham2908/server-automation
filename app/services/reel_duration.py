"""Reel lengths, read from the video file itself.

Instagram's API has no length field for reels, so the sync reads it from the
file with ``ffprobe`` — from our R2 copy when we uploaded the reel, otherwise
from Instagram's CDN URL. ``ffprobe`` reads only the container header, not the
whole video, and a reel's length never changes, so each reel is probed once.
"""

from __future__ import annotations

import json
import subprocess

from app.logger import get_logger

logger = get_logger(__name__)

PROBE_TIMEOUT_SECONDS = 30


def parse_ffprobe_duration(output: str) -> float | None:
    """The duration in seconds from ``ffprobe -show_entries format=duration -of json`` output."""
    try:
        body = json.loads(output)
    except json.JSONDecodeError:
        return None
    raw = (body.get("format") or {}).get("duration") if isinstance(body, dict) else None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def probe_duration_seconds(url: str) -> float | None:
    """Blocking; call in a thread. ``None`` (logged) when the file can't be read."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        # The URL carries a signature, so only the failure kind is logged.
        logger.warning("ffprobe could not read a reel's length: %s", type(exc).__name__)
        return None
    return parse_ffprobe_duration(result.stdout)
