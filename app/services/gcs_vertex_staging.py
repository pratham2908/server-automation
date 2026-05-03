"""Stage local video files in GCS for Vertex Gemini multimodal (``gs://`` URIs)."""

from __future__ import annotations

import uuid
from pathlib import Path

_VIDEO_MIME_BY_SUFFIX: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
}


def mime_type_for_video_path(local_path: str) -> str:
    suf = Path(local_path).suffix.lower()
    return _VIDEO_MIME_BY_SUFFIX.get(suf, "video/mp4")


def upload_local_video_for_vertex(bucket_name: str, local_path: str, *, prefix: str = "vertex-staging") -> str:
    """Upload *local_path* to *bucket_name* and return a ``gs://`` URI."""

    from google.cloud import storage

    client = storage.Client()
    ext = Path(local_path).suffix.lower() or ".mp4"
    blob_name = f"{prefix}/{uuid.uuid4().hex}{ext}"
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(local_path)
    return f"gs://{bucket_name}/{blob_name}"


def delete_gs_uri(gs_uri: str) -> None:
    """Delete the object referenced by *gs_uri* (``gs://bucket/object``)."""

    if not gs_uri.startswith("gs://"):
        return
    rest = gs_uri[5:]
    slash = rest.index("/")
    bucket_name, blob_name = rest[:slash], rest[slash + 1 :]
    from google.cloud import storage

    client = storage.Client()
    client.bucket(bucket_name).blob(blob_name).delete()
