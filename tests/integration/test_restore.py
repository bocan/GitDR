"""
Integration tests for the restore workflow.

Tests use a real local git repository and the local storage backend.
No network access is required.
"""

# ruff: noqa: S603, S604, S607
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from gitdr.database.models import BackupRun
from gitdr.services import git_ops
from gitdr.services.restore import _detect_format, _find_repo, run_restore
from gitdr.services.storage.local import LocalStorageBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bare_repo(path: Path) -> Path:
    """Create a bare git repo at *path* with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", str(path)], check=True, capture_output=True)
    return path


def _make_repo_with_commit(path: Path) -> Path:
    """Create a normal git repo with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
        cwd=str(path),
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        check=True,
        capture_output=True,
        cwd=str(path),
    )
    (path / "readme.txt").write_text("hello")
    subprocess.run(["git", "add", "."], check=True, capture_output=True, cwd=str(path))
    subprocess.run(
        ["git", "commit", "-m", "init"],
        check=True,
        capture_output=True,
        cwd=str(path),
    )
    return path


# ---------------------------------------------------------------------------
# _detect_format
# ---------------------------------------------------------------------------


def test_detect_format_bundle() -> None:
    assert _detect_format("gitdr/src/repo/ts.bundle") == "bundle"


def test_detect_format_tar_zst() -> None:
    assert _detect_format("gitdr/src/repo/ts.tar.zst") == "tar_zstd"


def test_detect_format_tar_zstd() -> None:
    assert _detect_format("gitdr/src/repo/ts.tar.zstd") == "tar_zstd"


def test_detect_format_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Cannot determine archive format"):
        _detect_format("gitdr/src/repo/ts.zip")


# ---------------------------------------------------------------------------
# _find_repo
# ---------------------------------------------------------------------------


def test_find_repo_bundle(tmp_path: Path) -> None:
    assert _find_repo(tmp_path, "bundle") == tmp_path


def test_find_repo_tar_with_dot_git(tmp_path: Path) -> None:
    dot_git = tmp_path / "myrepo.git"
    dot_git.mkdir()
    result = _find_repo(tmp_path, "tar_zstd")
    assert result == dot_git


def test_find_repo_tar_fallback(tmp_path: Path) -> None:
    # No .git directories — fall back to restore_dir itself
    result = _find_repo(tmp_path, "tar_zstd")
    assert result == tmp_path


# ---------------------------------------------------------------------------
# git_ops restore helpers (unit-level, no subprocess for bundle in unit tests)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not __import__("shutil").which("git"),
    reason="git not available",
)
def test_restore_bundle_round_trip(tmp_path: Path) -> None:
    """Create a bundle from a local repo, then clone it back."""
    src_repo = _make_repo_with_commit(tmp_path / "src_repo")

    # Clone as bare mirror
    mirror = tmp_path / "mirror.git"
    subprocess.run(
        ["git", "clone", "--mirror", str(src_repo), str(mirror)],
        check=True,
        capture_output=True,
    )

    # Create bundle
    bundle = tmp_path / "backup.bundle"
    git_ops.create_bundle(mirror, bundle)
    assert bundle.exists()
    assert bundle.stat().st_size > 0

    # Restore
    restore_dir = tmp_path / "restored"
    result = git_ops.restore_bundle(bundle, restore_dir)
    assert result == restore_dir
    assert (restore_dir / "readme.txt").exists()


@pytest.mark.skipif(
    not __import__("shutil").which("zstd"),
    reason="zstd not available",
)
def test_restore_tar_round_trip(tmp_path: Path) -> None:
    """Create a tar+zstd archive, then extract it back."""
    src_repo = _make_repo_with_commit(tmp_path / "src_repo")

    mirror = tmp_path / "mirror.git"
    subprocess.run(
        ["git", "clone", "--mirror", str(src_repo), str(mirror)],
        check=True,
        capture_output=True,
    )

    archive = tmp_path / "backup.tar.zst"
    git_ops.create_tar_archive(mirror, archive)
    assert archive.exists()

    restore_dir = tmp_path / "restored"
    result = git_ops.restore_tar_archive(archive, restore_dir)
    assert result == restore_dir
    # The extracted directory should contain mirror.git
    assert (restore_dir / "mirror.git").exists()


# ---------------------------------------------------------------------------
# run_restore end-to-end (mocked storage)
# ---------------------------------------------------------------------------


class _FakeStorage(LocalStorageBackend):
    """Local storage that pre-populates itself with a bundle for testing."""

    def __init__(self, root: Path, bundle_path: Path, remote_key: str) -> None:
        super().__init__(root)
        dest = root / remote_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.copy2(str(bundle_path), str(dest))


@pytest.mark.asyncio
@pytest.mark.skipif(
    not __import__("shutil").which("git"),
    reason="git not available",
)
async def test_run_restore_bundle(tmp_path: Path) -> None:
    """End-to-end: bundle -> upload to local storage -> run_restore."""
    # Build a local mirror + bundle
    src = _make_repo_with_commit(tmp_path / "src")
    mirror = tmp_path / "mirror.git"
    subprocess.run(
        ["git", "clone", "--mirror", str(src), str(mirror)],
        check=True,
        capture_output=True,
    )
    bundle = tmp_path / "backup.bundle"
    git_ops.create_bundle(mirror, bundle)

    remote_key = "gitdr/test-src/test-repo/20250101T000000_000000Z.bundle"
    storage_root = tmp_path / "storage"
    storage = _FakeStorage(storage_root, bundle, remote_key)

    run = BackupRun(
        id=uuid4(),
        job_id=uuid4(),
        repo_id=uuid4(),
        status="success",
        archive_path=remote_key,
    )

    restore_base = tmp_path / "restores"
    result, log = await run_restore(run, storage, restore_base)

    assert result.exists()
    assert (result / "readme.txt").exists()
    assert "Download complete" in log
    assert "git clone complete" in log


@pytest.mark.asyncio
async def test_run_restore_no_archive_path_raises() -> None:
    run = BackupRun(
        id=uuid4(),
        job_id=uuid4(),
        repo_id=uuid4(),
        status="success",
        archive_path=None,
    )
    storage = LocalStorageBackend(Path("/tmp"))  # noqa: S108
    with pytest.raises(ValueError, match="no archive_path"):
        await run_restore(run, storage, Path("/tmp"))  # noqa: S108


@pytest.mark.asyncio
async def test_run_restore_bad_format_raises(tmp_path: Path) -> None:
    remote_key = "gitdr/test-src/test-repo/20250101T000000_000000Z.zip"
    run = BackupRun(
        id=uuid4(),
        job_id=uuid4(),
        repo_id=uuid4(),
        status="success",
        archive_path=remote_key,
    )
    storage = LocalStorageBackend(tmp_path)
    with pytest.raises(ValueError, match="Cannot determine archive format"):
        await run_restore(run, storage, tmp_path)


@pytest.mark.asyncio
async def test_run_restore_checksum_mismatch_raises(tmp_path: Path) -> None:
    # Create a real file and upload it to local storage
    remote_key = "gitdr/test-src/test-repo/20250101T000000_000000Z.bundle"
    storage_root = tmp_path / "storage"
    storage = LocalStorageBackend(storage_root)
    archive = tmp_path / "fake.bundle"
    archive.write_bytes(b"not a real bundle")
    await storage.upload(archive, remote_key)

    run = BackupRun(
        id=uuid4(),
        job_id=uuid4(),
        repo_id=uuid4(),
        status="success",
        archive_path=remote_key,
        checksum_sha256="deadbeef" * 8,  # wrong checksum
    )
    restore_base = tmp_path / "restores"
    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        await run_restore(run, storage, restore_base)
