"""Unit tests for the retention enforcement service."""

import pytest

from gitdr.services.retention import enforce_retention


class MockStorage:
    """Simple in-memory storage stub."""

    def __init__(self, keys: list[str]) -> None:
        self._keys: list[str] = list(keys)
        self.deleted: list[str] = []

    async def list_keys(self, prefix: str) -> list[str]:
        return [k for k in self._keys if k.startswith(prefix)]

    async def delete(self, key: str) -> None:
        self._keys.remove(key)
        self.deleted.append(key)

    # Unused protocol methods
    async def upload(self, *a: object, **kw: object) -> None: ...
    async def download(self, *a: object, **kw: object) -> None: ...
    async def exists(self, *a: object, **kw: object) -> bool: ...


@pytest.mark.asyncio
async def test_retention_zero_keeps_all() -> None:
    """retention_count=0 means keep everything — no deletes."""
    storage = MockStorage(
        [
            "gitdr/mysrc/repo-a/20250101T000000_000000Z.bundle",
            "gitdr/mysrc/repo-a/20250102T000000_000000Z.bundle",
            "gitdr/mysrc/repo-a/20250103T000000_000000Z.bundle",
        ]
    )
    deleted = await enforce_retention(storage, "mysrc", "repo-a", 0)
    assert deleted == 0
    assert storage.deleted == []


@pytest.mark.asyncio
async def test_retention_within_limit_nothing_deleted() -> None:
    storage = MockStorage(
        [
            "gitdr/mysrc/repo-a/20250101T000000_000000Z.bundle",
            "gitdr/mysrc/repo-a/20250102T000000_000000Z.bundle",
        ]
    )
    deleted = await enforce_retention(storage, "mysrc", "repo-a", 5)
    assert deleted == 0
    assert storage.deleted == []


@pytest.mark.asyncio
async def test_retention_exactly_at_limit() -> None:
    storage = MockStorage(
        [
            "gitdr/mysrc/repo-a/20250101T000000_000000Z.bundle",
            "gitdr/mysrc/repo-a/20250102T000000_000000Z.bundle",
            "gitdr/mysrc/repo-a/20250103T000000_000000Z.bundle",
        ]
    )
    deleted = await enforce_retention(storage, "mysrc", "repo-a", 3)
    assert deleted == 0


@pytest.mark.asyncio
async def test_retention_deletes_oldest() -> None:
    """The two oldest archives should be removed."""
    storage = MockStorage(
        [
            "gitdr/mysrc/repo-a/20250101T000000_000000Z.bundle",
            "gitdr/mysrc/repo-a/20250102T000000_000000Z.bundle",
            "gitdr/mysrc/repo-a/20250103T000000_000000Z.bundle",
            "gitdr/mysrc/repo-a/20250104T000000_000000Z.bundle",
            "gitdr/mysrc/repo-a/20250105T000000_000000Z.bundle",
        ]
    )
    deleted = await enforce_retention(storage, "mysrc", "repo-a", 3)
    assert deleted == 2
    assert "gitdr/mysrc/repo-a/20250101T000000_000000Z.bundle" in storage.deleted
    assert "gitdr/mysrc/repo-a/20250102T000000_000000Z.bundle" in storage.deleted
    # Newer three remain
    assert len(storage._keys) == 3


@pytest.mark.asyncio
async def test_retention_only_matches_prefix() -> None:
    """Retention should only delete archives for the matching repo."""
    storage = MockStorage(
        [
            "gitdr/mysrc/repo-a/20250101T000000_000000Z.bundle",
            "gitdr/mysrc/repo-a/20250102T000000_000000Z.bundle",
            "gitdr/mysrc/repo-b/20250101T000000_000000Z.bundle",  # different repo
        ]
    )
    deleted = await enforce_retention(storage, "mysrc", "repo-a", 1)
    assert deleted == 1
    assert storage.deleted == ["gitdr/mysrc/repo-a/20250101T000000_000000Z.bundle"]
    # repo-b untouched
    assert "gitdr/mysrc/repo-b/20250101T000000_000000Z.bundle" in storage._keys


@pytest.mark.asyncio
async def test_retention_empty_store() -> None:
    storage = MockStorage([])
    deleted = await enforce_retention(storage, "mysrc", "repo-a", 3)
    assert deleted == 0


@pytest.mark.asyncio
async def test_retention_delete_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed delete logs but does not raise; other deletes still proceed."""

    class FlakyStorage(MockStorage):
        async def delete(self, key: str) -> None:
            if "20250101" in key:
                raise OSError("disk full")
            await super().delete(key)

    storage = FlakyStorage(
        [
            "gitdr/mysrc/repo-a/20250101T000000_000000Z.bundle",
            "gitdr/mysrc/repo-a/20250102T000000_000000Z.bundle",
            "gitdr/mysrc/repo-a/20250103T000000_000000Z.bundle",
        ]
    )
    # retention_count=1 → 2 to delete; first one fails, second succeeds
    deleted = await enforce_retention(storage, "mysrc", "repo-a", 1)
    assert deleted == 1  # only the successful one counted
    assert storage.deleted == ["gitdr/mysrc/repo-a/20250102T000000_000000Z.bundle"]
