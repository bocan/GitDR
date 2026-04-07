"""
Local filesystem storage backend for GitDR.

All blocking I/O is dispatched via ``asyncio.to_thread`` so the FastAPI event
loop is never blocked.
"""

import asyncio
import shutil
from pathlib import Path


class LocalStorageBackend:
    """
    Storage backend that persists archives to a local directory tree.

    Remote keys are treated as relative paths under *root*:

        root / "gitdr/github-myorg/api-service/20250406T120000Z.bundle"

    The root directory is created on instantiation if it does not exist.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Private sync helpers (called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _dest(self, remote_key: str) -> Path:
        return self.root / remote_key

    def _upload_sync(self, local_path: Path, remote_key: str) -> None:
        dest = self._dest(remote_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(local_path), str(dest))

    def _download_sync(self, remote_key: str, local_path: Path) -> None:
        src = self._dest(remote_key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(local_path))

    def _delete_sync(self, remote_key: str) -> None:
        self._dest(remote_key).unlink()

    def _list_sync(self, prefix: str) -> list[str]:
        search_dir = self.root / prefix
        if not search_dir.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in search_dir.rglob("*") if p.is_file())

    def _exists_sync(self, remote_key: str) -> bool:
        return self._dest(remote_key).is_file()

    # ------------------------------------------------------------------
    # StorageBackend Protocol implementation
    # ------------------------------------------------------------------

    async def upload(self, local_path: Path, remote_key: str) -> None:
        """Copy *local_path* to *remote_key* under the backend root."""
        await asyncio.to_thread(self._upload_sync, local_path, remote_key)

    async def download(self, remote_key: str, local_path: Path) -> None:
        """Fetch *remote_key* and write it to *local_path*."""
        await asyncio.to_thread(self._download_sync, remote_key, local_path)

    async def delete(self, remote_key: str) -> None:
        """Remove *remote_key* from the backend root."""
        await asyncio.to_thread(self._delete_sync, remote_key)

    async def list_keys(self, prefix: str) -> list[str]:
        """Return all keys (file paths relative to root) under *prefix*."""
        return await asyncio.to_thread(self._list_sync, prefix)

    async def exists(self, remote_key: str) -> bool:
        """Return True if *remote_key* exists under the backend root."""
        return await asyncio.to_thread(self._exists_sync, remote_key)
