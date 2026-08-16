"""Cloudflare R2 service – S3-compatible object storage for video files.

All operations stream data so that multi-GB files never sit fully in memory.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO

import boto3


class R2Service:
    """Thin wrapper around a boto3 S3 client pointed at Cloudflare R2."""

    def __init__(
        self,
        endpoint_url: str | None,
        access_key_id: str,
        secret_access_key: str,
        bucket_name: str,
        region_name: str = "auto",
    ) -> None:
        # endpoint_url is None for real AWS S3, where boto3 derives it from the
        # region. R2 (and MinIO-likes) always need an explicit endpoint, and use
        # the sentinel region "auto".
        self._bucket = bucket_name
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region_name,
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

    def file_exists(self, key: str) -> bool:
        """Check if *key* exists in the bucket."""
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise

    def generate_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """Generate a temporary GET URL for *key* (default 1 hour)."""
        from typing import cast

        return cast(
            str,
            self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            ),
        )

    def generate_presigned_put_url(self, key: str, expires_in: int = 900) -> str:
        """Generate a presigned PUT URL so the browser can upload directly to R2.

        NOTE: Your R2 bucket must allow CORS with PUT from the frontend origin.
        Default expiry is 15 minutes, enough for large video files on typical
        home connections.
        """
        from typing import cast

        return cast(
            str,
            self._client.generate_presigned_url(
                "put_object",
                Params={"Bucket": self._bucket, "Key": key, "ContentType": "video/mp4"},
                ExpiresIn=expires_in,
            ),
        )

    def list_objects_with_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """List object metadata under *prefix* (paginated)."""
        paginator = self._client.get_paginator("list_objects_v2")
        out: list[dict[str, Any]] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                lm = obj.get("LastModified")
                out.append(
                    {
                        "key": obj["Key"],
                        "size": int(obj["Size"]),
                        "last_modified": lm,
                    }
                )
        return out

    def list_objects_page(
        self,
        prefix: str = "",
        delimiter: str = "/",
        continuation_token: str | None = None,
        max_keys: int = 500,
    ) -> dict[str, Any]:
        """One page of a folder-style listing under *prefix*.

        Unlike :meth:`list_objects_with_prefix`, this collapses nested keys into
        ``folders`` via *delimiter* so the caller can render a browsable tree
        instead of pulling every key in the bucket.
        """
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix, "MaxKeys": max_keys}
        if delimiter:
            kwargs["Delimiter"] = delimiter
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token

        resp = self._client.list_objects_v2(**kwargs)

        files: list[dict[str, Any]] = []
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            # S3 represents an explicit "folder" as a zero-byte key ending in the
            # delimiter; it is not a real object the user can import.
            if key == prefix or key.endswith("/"):
                continue
            files.append(
                {
                    "key": key,
                    "size": int(obj["Size"]),
                    "last_modified": self._normalize_utc(obj.get("LastModified")),
                }
            )

        return {
            "folders": [cp["Prefix"] for cp in resp.get("CommonPrefixes", [])],
            "files": files,
            "next_cursor": resp.get("NextContinuationToken"),
            "is_truncated": bool(resp.get("IsTruncated", False)),
        }

    def head_object(self, key: str) -> dict[str, Any]:
        """Return size/content-type metadata for *key* without fetching it."""
        resp = self._client.head_object(Bucket=self._bucket, Key=key)
        return {
            "size": int(resp.get("ContentLength", 0)),
            "content_type": resp.get("ContentType", ""),
            "last_modified": self._normalize_utc(resp.get("LastModified")),
        }

    def download_to_path(self, key: str, dest_path: str) -> None:
        """Download *key* into an existing local path (caller owns the file)."""
        with open(dest_path, "wb") as fh:
            self._client.download_fileobj(self._bucket, key, fh)

    @staticmethod
    def _normalize_utc(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def count_purgeable(
        self,
        prefix: str,
        days_old: int,
        protected_keys: set[str] | None = None,
    ) -> tuple[int, int]:
        """Count objects and total bytes older than *days_old* under *prefix*.

        Skips keys in *protected_keys* so the estimate matches what purge will
        actually delete.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_old)
        n = 0
        total_b = 0
        for o in self.list_objects_with_prefix(prefix):
            lm = self._normalize_utc(o.get("last_modified"))
            key = o.get("key")
            if not key or lm is None or lm >= cutoff:
                continue
            if protected_keys and key in protected_keys:
                continue
            n += 1
            total_b += int(o.get("size", 0))
        return n, total_b

    def purge_prefix_older_than(
        self,
        prefix: str,
        days_old: int,
        protected_keys: set[str] | None = None,
    ) -> tuple[int, int]:
        """Delete objects under *prefix* older than *days_old* days.

        Skips any key present in *protected_keys* (i.e. still referenced by a
        video record in the DB).  Returns (purged, errors).
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_old)
        purged = 0
        errors = 0
        for o in self.list_objects_with_prefix(prefix):
            lm = self._normalize_utc(o.get("last_modified"))
            key = o.get("key")
            if not key or lm is None or lm >= cutoff:
                continue
            if protected_keys and key in protected_keys:
                continue
            try:
                self.delete_video(key)
                purged += 1
            except Exception:
                errors += 1
        return purged, errors
