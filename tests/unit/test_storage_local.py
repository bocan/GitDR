"""
Unit tests for gitdr.services.storage.local.LocalStorageBackend.

Uses pytest's ``tmp_path`` fixture for real filesystem operations so we can
verify the actual file-system behaviour without mocking.  All tests use
``anyio`` via the ``pytest-anyio`` mark to drive the async methods.
"""

import pytest

from gitdr.services.storage.local import LocalStorageBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    return LocalStorageBackend(tmp_path / "store")


@pytest.fixture
def sample_file(tmp_path) -> object:
    """A small binary file to upload."""
    p = tmp_path / "sample.bundle"
    p.write_bytes(b"fake git bundle data 1234")
    return p


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


class TestUpload:
    async def test_copies_file_to_remote_key(self, storage, sample_file):
        await storage.upload(sample_file, "gitdr/src/repo/20250101T000000Z.bundle")
        dest = storage.root / "gitdr/src/repo/20250101T000000Z.bundle"
        assert dest.exists()
        assert dest.read_bytes() == sample_file.read_bytes()

    async def test_creates_intermediate_directories(self, storage, sample_file):
        await storage.upload(sample_file, "a/b/c/d/e/f.bundle")
        assert (storage.root / "a/b/c/d/e/f.bundle").exists()

    async def test_overwrites_existing_key(self, storage, tmp_path):
        first = tmp_path / "first.bundle"
        first.write_bytes(b"original")
        await storage.upload(first, "key.bundle")

        second = tmp_path / "second.bundle"
        second.write_bytes(b"updated")
        await storage.upload(second, "key.bundle")

        assert (storage.root / "key.bundle").read_bytes() == b"updated"


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


class TestDownload:
    async def test_copies_file_to_local_path(self, storage, sample_file, tmp_path):
        await storage.upload(sample_file, "repo/archive.bundle")
        dest = tmp_path / "downloaded.bundle"
        await storage.download("repo/archive.bundle", dest)
        assert dest.read_bytes() == sample_file.read_bytes()

    async def test_creates_parent_dirs_for_download(self, storage, sample_file, tmp_path):
        await storage.upload(sample_file, "k.bundle")
        dest = tmp_path / "nested" / "out.bundle"
        await storage.download("k.bundle", dest)
        assert dest.exists()

    async def test_raises_for_missing_key(self, storage, tmp_path):
        with pytest.raises(FileNotFoundError):
            await storage.download("does/not/exist.bundle", tmp_path / "out.bundle")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_removes_file(self, storage, sample_file):
        await storage.upload(sample_file, "to-delete.bundle")
        assert (storage.root / "to-delete.bundle").exists()
        await storage.delete("to-delete.bundle")
        assert not (storage.root / "to-delete.bundle").exists()

    async def test_raises_for_missing_key(self, storage):
        with pytest.raises(FileNotFoundError):
            await storage.delete("does-not-exist.bundle")


# ---------------------------------------------------------------------------
# list_keys
# ---------------------------------------------------------------------------


class TestListKeys:
    async def test_returns_keys_under_prefix(self, storage, sample_file):
        await storage.upload(sample_file, "gitdr/src/repo1/a.bundle")
        await storage.upload(sample_file, "gitdr/src/repo1/b.bundle")
        await storage.upload(sample_file, "gitdr/src/repo2/c.bundle")
        await storage.upload(sample_file, "other/d.bundle")

        keys = await storage.list_keys("gitdr/src/repo1")
        assert sorted(keys) == [
            "gitdr/src/repo1/a.bundle",
            "gitdr/src/repo1/b.bundle",
        ]

    async def test_returns_empty_list_for_missing_prefix(self, storage):
        keys = await storage.list_keys("nonexistent/prefix")
        assert keys == []

    async def test_returns_all_files_recursively(self, storage, sample_file):
        await storage.upload(sample_file, "p/a/1.bundle")
        await storage.upload(sample_file, "p/b/2.bundle")

        keys = await storage.list_keys("p")
        assert len(keys) == 2
        assert all(k.startswith("p/") for k in keys)


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------


class TestExists:
    async def test_returns_true_for_existing_key(self, storage, sample_file):
        await storage.upload(sample_file, "exists.bundle")
        assert await storage.exists("exists.bundle") is True

    async def test_returns_false_for_missing_key(self, storage):
        assert await storage.exists("missing.bundle") is False


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_is_storage_backend_instance(self, storage):
        from gitdr.services.storage.base import StorageBackend

        assert isinstance(storage, StorageBackend)
