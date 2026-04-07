"""Storage backend implementations for GitDR."""

from pathlib import Path
from typing import Any

from gitdr.services.storage.base import StorageBackend


def build_storage_backend(dest_type: str, config: dict[str, Any]) -> StorageBackend:
    """
    Construct a StorageBackend from a decrypted destination config dict.

    Only ``local`` is fully implemented in Phase 4.  Cloud backends (s3,
    gcs, azure_blob) will be added in Phase 5 when the respective SDK
    wrappers are built out.
    """
    if dest_type == "local":
        from gitdr.services.storage.local import LocalStorageBackend

        root = Path(config.get("path", "/tmp/gitdr-backups"))  # noqa: S108
        return LocalStorageBackend(root)

    if dest_type == "s3":
        from gitdr.services.storage.s3 import S3StorageBackend

        return S3StorageBackend(config)

    if dest_type == "gcs":
        raise NotImplementedError("GCS storage backend is not yet implemented.")

    if dest_type == "azure_blob":
        raise NotImplementedError("Azure Blob storage backend is not yet implemented.")

    raise ValueError(f"Unknown destination type: {dest_type!r}")
