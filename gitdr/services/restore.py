"""
Restore orchestrator for GitDR.

``run_restore`` downloads an archive from the storage backend, reconstitutes
the repository in a temporary directory, and optionally pushes all refs to a
new remote.

Supported archive formats:
- ``bundle``   — restored via ``git clone <bundle>``
- ``tar_zstd`` — extracted via ``tar --use-compress-program=zstd``

Both leave a working local copy under ``restore_dir``.

Cleanup:
  The caller is responsible for deleting ``restore_dir`` after use when
  ``push_url`` is set.  For interactive/API use the endpoint should return
  the restore path and let the user decide, or arrange cleanup via a background
  task.
"""

import asyncio
import hashlib
import logging
import shutil
import tempfile
from pathlib import Path

from gitdr.database.models import BackupRun
from gitdr.services import git_ops
from gitdr.services.storage.base import StorageBackend

logger = logging.getLogger(__name__)


def _sha256_file(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65_536), b""):
            sha.update(chunk)
    return sha.hexdigest()


async def run_restore(
    run: BackupRun,
    storage: StorageBackend,
    restore_base: Path,
    *,
    push_url: str | None = None,
) -> tuple[Path, str]:
    """
    Download and reconstitute a backup archive from *run*.

    Parameters
    ----------
    run:
        A successful ``BackupRun`` record.  ``archive_path`` must be set.
    storage:
        The storage backend that holds the archive.
    restore_base:
        Parent directory under which the restore directory is created.
        A timestamped sub-directory is created automatically.
    push_url:
        Optional URL to push all restored refs to. Must use ``https://`` or
        ``ssh://``.  When supplied the restored repo is pushed then the local
        copy is removed.

    Returns
    -------
    tuple[Path, str]
        ``(restore_dir, log_output)`` — path to the restored directory and a
        human-readable log of what was done.  ``restore_dir`` will be empty
        (cleaned up) if *push_url* was provided and the push succeeded.

    Raises
    ------
    ValueError
        If ``run.archive_path`` is not set, or the format cannot be determined.
    RuntimeError
        If the downloaded archive's checksum does not match the recorded value.
    subprocess.CalledProcessError
        If any git or tar command fails.
    """
    if not run.archive_path:
        raise ValueError(f"BackupRun {run.id} has no archive_path — cannot restore")

    archive_key = run.archive_path
    archive_format = _detect_format(archive_key)
    log_lines: list[str] = []

    def _log(msg: str) -> None:
        logger.info(msg)
        log_lines.append(msg)

    # ------------------------------------------------------------------
    # 1. Download archive to a temp file
    # ------------------------------------------------------------------
    restore_base.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(dir=restore_base))
    ext = "bundle" if archive_format == "bundle" else "tar.zst"
    tmp_archive = tmp_dir / f"archive.{ext}"

    try:
        _log(f"Downloading archive: {archive_key}")
        await storage.download(archive_key, tmp_archive)
        _log(f"  Download complete ({tmp_archive.stat().st_size:,} bytes)")

        # ------------------------------------------------------------------
        # 2. Verify checksum if recorded
        # ------------------------------------------------------------------
        if run.checksum_sha256:
            _log("Verifying SHA-256 checksum…")
            actual = await asyncio.to_thread(_sha256_file, tmp_archive)
            if actual != run.checksum_sha256:
                raise RuntimeError(
                    f"Checksum mismatch for run {run.id}: "
                    f"expected {run.checksum_sha256!r}, got {actual!r}"
                )
            _log(f"  Checksum OK: {actual}")

        # ------------------------------------------------------------------
        # 3. Reconstitute
        # ------------------------------------------------------------------
        restore_dir = tmp_dir / "restored"

        if archive_format == "bundle":
            _log(f"Cloning from bundle → {restore_dir}")
            await asyncio.to_thread(git_ops.restore_bundle, tmp_archive, restore_dir)
            _log("  git clone complete")
        else:
            _log(f"Extracting tar+zstd archive → {restore_dir}")
            await asyncio.to_thread(git_ops.restore_tar_archive, tmp_archive, restore_dir)
            _log("  extraction complete")

        _log(f"Restore directory: {restore_dir}")

        # ------------------------------------------------------------------
        # 4. Optionally push to a new remote
        # ------------------------------------------------------------------
        if push_url:
            _log(f"Pushing to remote: {push_url}")
            # For tar archives the restore_dir contains <repo_name>.git;
            # find the bare repo to push from.
            repo_path = _find_repo(restore_dir, archive_format)
            await asyncio.to_thread(git_ops.push_to_remote, repo_path, push_url)
            _log("  Push complete")
            # Clean up the entire temp directory (archive + restored repo)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            _log("  Temporary files cleaned up")
        else:
            # Keep the restored repo but remove the downloaded archive file
            tmp_archive.unlink(missing_ok=True)
            _log(f"  Archive file removed; restored repo kept at: {restore_dir}")

        return restore_dir, "\n".join(log_lines)

    except Exception:
        # Best-effort cleanup of the entire temp dir on failure
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_format(archive_key: str) -> str:
    """Return ``'bundle'`` or ``'tar_zstd'`` based on the archive key extension."""
    if archive_key.endswith(".bundle"):
        return "bundle"
    if archive_key.endswith((".tar.zst", ".tar.zstd")):
        return "tar_zstd"
    raise ValueError(
        f"Cannot determine archive format from key {archive_key!r}. "
        "Expected .bundle or .tar.zst extension."
    )


def _find_repo(restore_dir: Path, archive_format: str) -> Path:
    """
    Return the path to the git repository within *restore_dir*.

    - bundle → restore_dir itself (git clone creates a populated repo there)
    - tar    → the first .git bare repo found one level below restore_dir
    """
    if archive_format == "bundle":
        return restore_dir
    # tar extraction puts <repo_name>.git directly under restore_dir
    candidates = sorted(restore_dir.glob("*.git"))
    if candidates:
        return candidates[0]
    # fall back to restore_dir if no .git directory found
    return restore_dir
