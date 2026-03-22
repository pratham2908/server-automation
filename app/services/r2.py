"""Cloudflare R2 service – S3-compatible object storage for video files.

All operations stream data so that multi-GB files never sit fully in memory.
"""

import tempfile
from typing import BinaryIO

import boto3


class R2Service:
    """Thin wrapper around a boto3 S3 client pointed at Cloudflare R2."""

    def __init__(
        self,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket_name: str,
    ) -> None:
        self._bucket = bucket_name
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upload_video(self, file_stream: BinaryIO, key: str) -> None:
        """Stream *file_stream* into R2 at *key* (e.g. ``ch1/vid.mp4``)."""
        self._client.upload_fileobj(file_stream, self._bucket, key)

    def download_video(self, key: str) -> str:
        """Download *key* from R2 into a temporary file and return its path.

        The caller is responsible for deleting the temp file after use.
        """
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        self._client.download_fileobj(self._bucket, key, tmp)
        tmp.close()
        return tmp.name

    def delete_video(self, key: str) -> None:
        """Delete *key* from R2."""
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def copy_video(self, source_key: str, dest_key: str) -> None:
        """Copy *source_key* to *dest_key* directly inside the bucket."""
        self._client.copy_object(
            CopySource={"Bucket": self._bucket, "Key": source_key},
            Bucket=self._bucket,
            Key=dest_key,
        )

    def generate_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """Generate a temporary public URL for *key* (default 1 hour)."""
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )
