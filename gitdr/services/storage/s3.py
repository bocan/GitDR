"""
S3 (and S3-compatible) storage backend for GitDR.

Uses ``boto3`` for all S3 API calls, dispatched via ``asyncio.to_thread`` so
the FastAPI event loop is never blocked.

Expected config keys (decrypted from ``BackupDestination.config``):

    {
        "bucket":            "my-backup-bucket",          # required
        "prefix":            "gitdr/",                    # optional, default ""
        "region":            "us-east-1",                 # optional
        "endpoint_url":      "https://s3.example.com",    # optional, S3-compat
        "access_key_id":     "AKIA...",                   # optional (role auth if absent)
        "secret_access_key": "...",                       # optional
    }

Security notes:
- ``endpoint_url`` must use ``https://`` if set; validated in ``__init__``.
- Credentials are never logged.
"""

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class S3StorageBackend:
    """
    S3 (or S3-compatible) storage backend.

    Parameters
    ----------
    config:
        Decrypted destination config dict.  See module docstring for keys.
    """

    def __init__(self, config: dict) -> None:  # type: ignore[type-arg]
        self.bucket: str = config["bucket"]
        self.prefix: str = config.get("prefix", "")
        region: str | None = config.get("region")
        endpoint_url: str | None = config.get("endpoint_url")
        access_key_id: str | None = config.get("access_key_id")
        secret_access_key: str | None = config.get("secret_access_key")

        # Enforce HTTPS on custom endpoints (OWASP: TLS for data in transit)
        if endpoint_url:
            parsed = urlparse(endpoint_url)
            if parsed.scheme != "https":
                raise ValueError(
                    f"S3 endpoint_url must use https:// (got {endpoint_url!r}). "
                    "Plain HTTP endpoints are not allowed."
                )

        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("boto3 is not installed. Add 'boto3' to your dependencies.") from exc

        kwargs: dict = {}  # type: ignore[type-arg]
        if region:
            kwargs["region_name"] = region
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if access_key_id and secret_access_key:
            kwargs["aws_access_key_id"] = access_key_id
            kwargs["aws_secret_access_key"] = secret_access_key

        self._client = boto3.client("s3", **kwargs)

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def _full_key(self, remote_key: str) -> str:
        """Prepend the configured prefix to *remote_key*."""
        if self.prefix:
            return f"{self.prefix.rstrip('/')}/{remote_key}"
        return remote_key

    def _strip_prefix(self, full_key: str) -> str:
        """Remove the configured prefix from a key returned by S3."""
        if self.prefix:
            stripped = self.prefix.rstrip("/") + "/"
            if full_key.startswith(stripped):
                return full_key[len(stripped) :]
        return full_key

    # ------------------------------------------------------------------
    # Sync operations (run via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _upload_sync(self, local_path: Path, remote_key: str) -> None:
        full = self._full_key(remote_key)
        logger.info("S3 upload: %s -> s3://%s/%s", local_path, self.bucket, full)
        self._client.upload_file(str(local_path), self.bucket, full)

    def _download_sync(self, remote_key: str, local_path: Path) -> None:
        full = self._full_key(remote_key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("S3 download: s3://%s/%s -> %s", self.bucket, full, local_path)
        self._client.download_file(self.bucket, full, str(local_path))

    def _delete_sync(self, remote_key: str) -> None:
        full = self._full_key(remote_key)
        logger.info("S3 delete: s3://%s/%s", self.bucket, full)
        self._client.delete_object(Bucket=self.bucket, Key=full)

    def _list_sync(self, prefix: str) -> list[str]:
        full_prefix = self._full_key(prefix)
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                keys.append(self._strip_prefix(obj["Key"]))
        return sorted(keys)

    def _exists_sync(self, remote_key: str) -> bool:
        full = self._full_key(remote_key)
        try:
            self._client.head_object(Bucket=self.bucket, Key=full)
            return True
        except self._client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] == "404":
                return False
            raise

    # ------------------------------------------------------------------
    # StorageBackend Protocol implementation
    # ------------------------------------------------------------------

    async def upload(self, local_path: Path, remote_key: str) -> None:
        await asyncio.to_thread(self._upload_sync, local_path, remote_key)

    async def download(self, remote_key: str, local_path: Path) -> None:
        await asyncio.to_thread(self._download_sync, remote_key, local_path)

    async def delete(self, remote_key: str) -> None:
        await asyncio.to_thread(self._delete_sync, remote_key)

    async def list_keys(self, prefix: str) -> list[str]:
        return await asyncio.to_thread(self._list_sync, prefix)

    async def exists(self, remote_key: str) -> bool:
        return await asyncio.to_thread(self._exists_sync, remote_key)
