"""
Storage backend Protocol for GitDR.

All storage backends implement this interface so the backup orchestrator
is decoupled from the concrete storage technology (local, S3, Azure Blob, etc.).
"""

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """
    Async protocol for all GitDR storage backends.

    Remote keys follow the convention:
        <prefix>/<source_name>/<repo_name>/<timestamp>.<format>

    Example: ``gitdr/github-myorg/api-service/20250406T120000Z.bundle``
    """

    async def upload(self, local_path: Path, remote_key: str) -> None:
        """Copy *local_path* to *remote_key* on the backend."""
        ...

    async def download(self, remote_key: str, local_path: Path) -> None:
        """Fetch *remote_key* from the backend and write it to *local_path*."""
        ...

    async def delete(self, remote_key: str) -> None:
        """Remove *remote_key* from the backend."""
        ...

    async def list_keys(self, prefix: str) -> list[str]:
        """Return all keys under *prefix* (non-recursive listing including sub-paths)."""
        ...

    async def exists(self, remote_key: str) -> bool:
        """Return True if *remote_key* exists on the backend."""
        ...
