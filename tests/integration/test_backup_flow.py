"""
Integration tests for the full backup flow.

These tests use real ``git`` subprocess calls against temporary local
repositories.  No network access is needed.  SQLCipher is NOT required -
we reuse the plain-SQLite in-memory engine from conftest.py.

URL validation is patched so that local file paths are accepted in place of
real forge URLs.  This lets us exercise the complete clone → archive → upload
→ BackupRun pipeline without a live remote.
"""

import json
import subprocess
from unittest.mock import patch
from uuid import uuid4

import pytest

from gitdr.config import Settings
from gitdr.database.models import (
    BackupDestination,
    BackupJob,
    GitSource,
    Repository,
)
from gitdr.services.backup import run_backup_job
from gitdr.services.storage.local import LocalStorageBackend

# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_config_env(monkeypatch, tmp_path):
    """
    Set minimal git user config so commits work inside CI / fresh machines
    without a global ~/.gitconfig.
    """
    monkeypatch.setenv("GIT_AUTHOR_NAME", "GitDR Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@gitdr.local")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "GitDR Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@gitdr.local")


@pytest.fixture
def source_repo(tmp_path, git_config_env):
    """
    A real local git repository with one commit, used as the backup source.
    """
    repo = tmp_path / "source-repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)  # noqa: S603, S607
    (repo / "README.md").write_text("# GitDR Integration Test")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)  # noqa: S603, S607
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "commit", "-m", "initial commit"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture
def backup_settings(tmp_path) -> Settings:
    """Settings pointing all paths at tmp_path sub-directories."""
    return Settings(
        gitdr_db_passphrase="integration-test-passphrase",
        gitdr_db_path=tmp_path / "data" / "gitdr.db",
        gitdr_cache_dir=tmp_path / "data" / "mirror-cache",
        gitdr_temp_dir=tmp_path / "data" / "tmp",
    )


@pytest.fixture
def storage(tmp_path) -> LocalStorageBackend:
    return LocalStorageBackend(tmp_path / "storage")


# ---------------------------------------------------------------------------
# DB record helpers
# ---------------------------------------------------------------------------


def _make_source(session, *, name: str = "test-source") -> GitSource:
    src = GitSource(
        name=name,
        forge_type="generic",
        base_url="https://example.com",
        auth_type="pat",
        auth_credential=b"dummy-encrypted-cred",
    )
    session.add(src)
    session.commit()
    session.refresh(src)
    return src


def _make_repo(
    session,
    source: GitSource,
    *,
    clone_url: str,
    repo_name: str = "test-repo",
    **kwargs,
) -> Repository:
    repo = Repository(
        source_id=source.id,
        repo_name=repo_name,
        clone_url=clone_url,
        **kwargs,
    )
    session.add(repo)
    session.commit()
    session.refresh(repo)
    return repo


def _make_destination(session, *, name: str = "local-dest") -> BackupDestination:
    dest = BackupDestination(
        name=name,
        dest_type="local",
        config=b"dummy-encrypted-config",
    )
    session.add(dest)
    session.commit()
    session.refresh(dest)
    return dest


def _make_job(
    session,
    source: GitSource,
    destination: BackupDestination,
    *,
    archive_format: str = "bundle",
    backup_type: str = "mirror",
    branch_filter: str | None = None,
    include_archived: bool = False,
) -> BackupJob:
    job = BackupJob(
        name="test-job",
        source_id=source.id,
        destination_id=destination.id,
        archive_format=archive_format,
        backup_type=backup_type,
        branch_filter=branch_filter,
        include_archived=include_archived,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


# ---------------------------------------------------------------------------
# Full backup flow - bundle format
# ---------------------------------------------------------------------------


class TestBackupFlowBundle:
    async def test_successful_bundle_backup(
        self, db_session, source_repo, backup_settings, storage
    ):
        source = _make_source(db_session)
        _make_repo(db_session, source, clone_url=str(source_repo), repo_name="test-repo")
        dest = _make_destination(db_session)
        job = _make_job(db_session, source, dest, archive_format="bundle")

        # Bypass URL validation so the local path is accepted as a clone URL
        with patch("gitdr.services.git_ops.validate_clone_url"):
            runs = await run_backup_job(job, db_session, storage, backup_settings, trigger="manual")

        assert len(runs) == 1
        run = runs[0]
        assert run.status == "success"
        assert run.trigger == "manual"
        assert run.size_bytes is not None and run.size_bytes > 0
        assert run.checksum_sha256 is not None and len(run.checksum_sha256) == 64
        assert run.archive_path is not None
        assert run.archive_path.endswith(".bundle")
        assert run.ref_manifest is not None
        refs = json.loads(run.ref_manifest)
        assert isinstance(refs, dict)
        assert len(refs) > 0

    async def test_archive_file_exists_in_storage(
        self, db_session, source_repo, backup_settings, storage
    ):
        source = _make_source(db_session)
        _make_repo(db_session, source, clone_url=str(source_repo))
        dest = _make_destination(db_session)
        job = _make_job(db_session, source, dest, archive_format="bundle")

        with patch("gitdr.services.git_ops.validate_clone_url"):
            runs = await run_backup_job(job, db_session, storage, backup_settings)

        run = runs[0]
        assert await storage.exists(run.archive_path)

    async def test_mirror_cache_persisted(self, db_session, source_repo, backup_settings, storage):
        source = _make_source(db_session)
        _make_repo(db_session, source, clone_url=str(source_repo))
        dest = _make_destination(db_session)
        job = _make_job(db_session, source, dest)

        with patch("gitdr.services.git_ops.validate_clone_url"):
            await run_backup_job(job, db_session, storage, backup_settings)

        mirror = backup_settings.gitdr_cache_dir / str(job.source_id) / "test-repo.git"
        assert mirror.exists()
        assert mirror.is_dir()

    async def test_temp_dir_cleaned_up_after_backup(
        self, db_session, source_repo, backup_settings, storage
    ):
        source = _make_source(db_session)
        _make_repo(db_session, source, clone_url=str(source_repo))
        dest = _make_destination(db_session)
        job = _make_job(db_session, source, dest)

        with patch("gitdr.services.git_ops.validate_clone_url"):
            await run_backup_job(job, db_session, storage, backup_settings)

        # Temp dir must exist (it's created as needed) but no leftover files
        if backup_settings.gitdr_temp_dir.exists():
            leftover = list(backup_settings.gitdr_temp_dir.iterdir())
            assert leftover == [], f"Unexpected leftover temp files: {leftover}"

    async def test_incremental_update_on_second_run(
        self, db_session, source_repo, backup_settings, storage
    ):
        source = _make_source(db_session)
        _make_repo(db_session, source, clone_url=str(source_repo))
        dest = _make_destination(db_session)
        job = _make_job(db_session, source, dest)

        with patch("gitdr.services.git_ops.validate_clone_url"):
            runs1 = await run_backup_job(job, db_session, storage, backup_settings)
            runs2 = await run_backup_job(job, db_session, storage, backup_settings)

        # Both runs should succeed (second uses update_mirror, not clone_mirror)
        assert runs1[0].status == "success"
        assert runs2[0].status == "success"
        # Two separate archives should exist
        keys = await storage.list_keys("gitdr")
        assert len(keys) == 2


# ---------------------------------------------------------------------------
# Tar+zstd format
# ---------------------------------------------------------------------------


class TestBackupFlowTarZstd:
    async def test_successful_tar_backup(self, db_session, source_repo, backup_settings, storage):
        source = _make_source(db_session)
        _make_repo(db_session, source, clone_url=str(source_repo))
        dest = _make_destination(db_session)
        job = _make_job(db_session, source, dest, archive_format="tar_zstd")

        with patch("gitdr.services.git_ops.validate_clone_url"):
            runs = await run_backup_job(job, db_session, storage, backup_settings)

        run = runs[0]
        assert run.status == "success"
        assert run.archive_path.endswith(".tar.zst")
        assert await storage.exists(run.archive_path)


# ---------------------------------------------------------------------------
# Selective backup mode
# ---------------------------------------------------------------------------


class TestBackupFlowSelective:
    async def test_selective_backup_succeeds(
        self, db_session, source_repo, backup_settings, storage
    ):
        source = _make_source(db_session)
        _make_repo(db_session, source, clone_url=str(source_repo))
        dest = _make_destination(db_session)
        job = _make_job(
            db_session,
            source,
            dest,
            backup_type="selective",
            branch_filter=json.dumps(["main", "master"]),
        )

        with patch("gitdr.services.git_ops.validate_clone_url"):
            runs = await run_backup_job(job, db_session, storage, backup_settings)

        assert runs[0].status == "success"


# ---------------------------------------------------------------------------
# Excluded and archived repos
# ---------------------------------------------------------------------------


class TestExcludedAndArchivedRepos:
    async def test_included_repos_filters_selection(
        self, db_session, source_repo, backup_settings, storage
    ):
        """Repos not in job.included_repos are skipped entirely."""
        source = _make_source(db_session)
        _make_repo(db_session, source, clone_url=str(source_repo), repo_name="org/kept")
        _make_repo(db_session, source, clone_url=str(source_repo), repo_name="org/skipped")
        dest = _make_destination(db_session)
        job = _make_job(db_session, source, dest)
        job.included_repos = json.dumps(["org/kept"])
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        with patch("gitdr.services.git_ops.validate_clone_url"):
            runs = await run_backup_job(job, db_session, storage, backup_settings)

        # Only the explicitly included repo is processed
        assert len(runs) == 1
        assert runs[0].status == "success"

    async def test_archived_repo_skipped_by_default(
        self, db_session, source_repo, backup_settings, storage
    ):
        source = _make_source(db_session)
        _make_repo(db_session, source, clone_url=str(source_repo), is_archived=True)
        dest = _make_destination(db_session)
        job = _make_job(db_session, source, dest, include_archived=False)

        with patch("gitdr.services.git_ops.validate_clone_url"):
            runs = await run_backup_job(job, db_session, storage, backup_settings)

        assert len(runs) == 1
        assert runs[0].status == "skipped"

    async def test_archived_repo_backed_up_when_opted_in(
        self, db_session, source_repo, backup_settings, storage
    ):
        source = _make_source(db_session)
        _make_repo(db_session, source, clone_url=str(source_repo), is_archived=True)
        dest = _make_destination(db_session)
        job = _make_job(db_session, source, dest, include_archived=True)

        with patch("gitdr.services.git_ops.validate_clone_url"):
            runs = await run_backup_job(job, db_session, storage, backup_settings)

        assert runs[0].status == "success"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    async def test_run_marked_failed_on_git_error(
        self, db_session, source_repo, backup_settings, storage
    ):
        source = _make_source(db_session)
        _make_repo(
            db_session,
            source,
            clone_url="https://does-not-exist.example.com/repo.git",
            repo_name="bad-repo",
        )
        dest = _make_destination(db_session)
        job = _make_job(db_session, source, dest)

        # Do NOT patch validate_clone_url - the URL is https:// so it passes.
        # The git clone will fail because the host doesn't exist.
        import subprocess as _sp

        with patch(
            "gitdr.services.git_ops.subprocess.run",
            side_effect=_sp.CalledProcessError(1, "git", stderr=b"fatal: not found"),
        ):
            runs = await run_backup_job(job, db_session, storage, backup_settings)

        run = runs[0]
        assert run.status == "failed"
        assert run.error_message is not None
        assert run.completed_at is not None
        assert run.duration_secs is not None

    async def test_missing_source_raises(self, db_session, backup_settings, storage):
        dest = _make_destination(db_session)
        # Build an in-memory job with a non-existent source_id.
        # We do NOT commit it to the DB because:
        #   a) run_backup_job raises before creating any BackupRun, and
        #   b) the FK constraint on backup_jobs.source_id would prevent INSERT.
        orphan_job = BackupJob(
            id=uuid4(),
            name="orphan",
            source_id=uuid4(),  # not in git_sources
            destination_id=dest.id,
        )

        with pytest.raises(ValueError, match="not found"):
            await run_backup_job(orphan_job, db_session, storage, backup_settings)
