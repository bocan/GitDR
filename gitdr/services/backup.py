"""
Backup orchestrator for GitDR.

``run_backup_job`` is the top-level async entry point called by the scheduler
and by manual-trigger API endpoints.  It iterates every non-excluded repository
in a job's source, clones / updates the mirror, creates an archive, uploads it
to storage, and writes a ``BackupRun`` record for each repository.

Status transitions per run: pending → running → success | failed | skipped
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from gitdr.config import Settings
from gitdr.database.models import BackupJob, BackupRun, GitSource, Repository
from gitdr.services import git_ops
from gitdr.services import retention as retention_svc
from gitdr.services.storage.base import StorageBackend

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _remote_key(
    source_name: str,
    repo_name: str,
    timestamp: datetime,
    archive_format: str,
) -> str:
    """
    Build the remote storage key for an archive.

    Format: ``gitdr/<source_name>/<repo_name>/<timestamp>.<ext>``

    Microseconds are included so that rapid successive backups (e.g. in
    tests or manual triggers) never collide on the same key.
    """
    ts = timestamp.strftime("%Y%m%dT%H%M%S_%fZ")
    ext = "bundle" if archive_format == "bundle" else "tar.zst"
    return f"gitdr/{source_name}/{repo_name}/{ts}.{ext}"


def _sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of *path*."""
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65_536), b""):
            sha.update(chunk)
    return sha.hexdigest()


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------


async def run_backup_job(
    job: BackupJob,
    session: Session,
    storage: StorageBackend,
    settings: Settings,
    *,
    trigger: str = "manual",
) -> list[BackupRun]:
    """
    Execute a backup job, producing one ``BackupRun`` per eligible repository.

    If ``job.included_repos`` is set, only those repos are processed.
    Archived repositories are skipped (status ``"skipped"``) unless
    ``job.include_archived`` is True.

    The function is safe to await from an async context; all blocking git and
    file-system work is dispatched through ``asyncio.to_thread``.

    Returns the list of ``BackupRun`` objects (one per repo processed).
    """
    source: GitSource | None = session.get(GitSource, job.source_id)
    if source is None:
        raise ValueError(f"GitSource {job.source_id} not found")

    repos = session.exec(select(Repository).where(Repository.source_id == job.source_id)).all()

    # Apply per-job repo selection if set (explicit list of repo_name strings)
    if job.included_repos:
        included: set[str] = set(json.loads(job.included_repos))
        if included:
            repos = [r for r in repos if r.repo_name in included]

    runs: list[BackupRun] = []

    for repo in repos:
        # ------------------------------------------------------------------
        # Skip archived repos unless the job opts in
        # ------------------------------------------------------------------
        if repo.is_archived and not job.include_archived:
            skipped = BackupRun(
                job_id=job.id,
                repo_id=repo.id,
                status="skipped",
                trigger=trigger,
            )
            session.add(skipped)
            session.commit()
            session.refresh(skipped)
            runs.append(skipped)
            continue

        # ------------------------------------------------------------------
        # Create the run record in pending state
        # ------------------------------------------------------------------
        run = BackupRun(
            job_id=job.id,
            repo_id=repo.id,
            status="pending",
            trigger=trigger,
        )
        session.add(run)
        session.commit()
        session.refresh(run)

        started = _utc_now()
        run.status = "running"
        run.started_at = started
        session.add(run)
        session.commit()

        tmp_archive: Path | None = None
        mirror_copy_root: Path | None = None
        log_lines: list[str] = []

        try:
            # ----------------------------------------------------------
            # 1. Clone or update the persistent mirror cache
            # ----------------------------------------------------------
            settings.gitdr_cache_dir.mkdir(parents=True, exist_ok=True)
            settings.gitdr_temp_dir.mkdir(parents=True, exist_ok=True)

            mirror = await asyncio.to_thread(
                git_ops.clone_or_update_mirror,
                repo.clone_url,
                str(job.source_id),
                repo.repo_name,
                settings.gitdr_cache_dir,
                settings.gitdr_temp_dir,
                log_lines,
            )

            # ----------------------------------------------------------
            # 2. Capture ref manifest
            # ----------------------------------------------------------
            refs = await asyncio.to_thread(git_ops.list_mirror_refs, mirror)
            log_lines.append(f"  [{len(refs)} refs captured]")

            # ----------------------------------------------------------
            # 3. Selective mode: work from a pruned temporary copy
            # ----------------------------------------------------------
            archive_source = mirror
            if job.backup_type == "selective" and job.branch_filter:
                patterns: list[str] = json.loads(job.branch_filter)
                mirror_copy_root = Path(tempfile.mkdtemp(dir=settings.gitdr_temp_dir))
                mirror_copy = mirror_copy_root / f"{repo.repo_name}.git"
                await asyncio.to_thread(shutil.copytree, str(mirror), str(mirror_copy))
                await asyncio.to_thread(git_ops.prune_refs, mirror_copy, patterns, log_lines)
                archive_source = mirror_copy

            # ----------------------------------------------------------
            # 4. Create archive in a temp file
            # ----------------------------------------------------------
            ext = "bundle" if job.archive_format == "bundle" else "tar.zst"
            tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=f".{ext}", dir=settings.gitdr_temp_dir)
            os.close(tmp_fd)
            tmp_archive = Path(tmp_path_str)

            if job.archive_format == "bundle":
                await asyncio.to_thread(
                    git_ops.create_bundle, archive_source, tmp_archive, log_lines
                )
            else:
                await asyncio.to_thread(
                    git_ops.create_tar_archive, archive_source, tmp_archive, log_lines
                )

            size_bytes = tmp_archive.stat().st_size
            checksum = await asyncio.to_thread(_sha256_file, tmp_archive)

            # ----------------------------------------------------------
            # 5. Upload to storage backend
            # ----------------------------------------------------------
            ts = _utc_now()
            remote_key = _remote_key(source.name, repo.repo_name, ts, job.archive_format)
            await storage.upload(tmp_archive, remote_key)
            log_lines.append(f"  [uploaded to {remote_key}]")

            # ----------------------------------------------------------
            # 5b. Enforce retention policy
            # ----------------------------------------------------------
            if job.retention_count > 0:
                deleted = await retention_svc.enforce_retention(
                    storage,
                    source.name,
                    repo.repo_name,
                    job.retention_count,
                )
                if deleted:
                    log_lines.append(f"  [retention: deleted {deleted} old archive(s)]")

            # ----------------------------------------------------------
            # 6. Record success
            # ----------------------------------------------------------
            completed = _utc_now()
            run.status = "success"
            run.completed_at = completed
            run.duration_secs = (completed - started).total_seconds()
            run.size_bytes = size_bytes
            run.archive_path = remote_key
            run.ref_manifest = json.dumps(refs)
            run.checksum_sha256 = checksum

        except Exception as exc:
            logger.error(
                "Backup failed for repo %r in job %s: %s",
                repo.repo_name,
                job.id,
                exc,
            )
            completed = _utc_now()
            run.status = "failed"
            run.completed_at = completed
            run.duration_secs = (completed - started).total_seconds()
            run.error_message = str(exc)
            log_lines.append(f"  [FAILED] {exc}")

        finally:
            # Always clean up temp files and mirror copies
            if tmp_archive is not None and tmp_archive.exists():
                tmp_archive.unlink(missing_ok=True)
            if mirror_copy_root is not None and mirror_copy_root.exists():
                shutil.rmtree(mirror_copy_root, ignore_errors=True)

        run.log_output = "\n".join(log_lines) if log_lines else None

        session.add(run)
        session.commit()
        session.refresh(run)
        runs.append(run)

    return runs
