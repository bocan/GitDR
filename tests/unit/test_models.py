"""
Unit tests for gitdr.database.models.

Uses a plain SQLite in-memory engine (db_engine fixture from conftest.py).
SQLCipher is not required for these tests; the model correctness is
independent of the encryption layer.

Foreign keys are enabled in the test engine so that FK constraints,
uniqueness, and ON DELETE CASCADE behaviour are exercised.
"""

from datetime import UTC
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from gitdr.database.models import (
    BackupDestination,
    BackupJob,
    BackupRun,
    GitSource,
    Repository,
    _UUIDString,
)

# ---------------------------------------------------------------------------
# _UUIDString TypeDecorator
# ---------------------------------------------------------------------------


class TestUUIDStringTypeDecorator:
    """Cover the bind-param conversion paths not exercised by model CRUD tests."""

    def test_none_returns_none(self):
        col = _UUIDString()
        assert col.process_bind_param(None, None) is None

    def test_uuid_object_returns_hex_no_hyphens(self):
        col = _UUIDString()
        uid = UUID("f6e9adfd-404a-43bf-a457-ac4ca4eb3f75")
        result = col.process_bind_param(uid, None)
        assert result == "f6e9adfd404a43bfa457ac4ca4eb3f75"
        assert "-" not in result

    def test_hyphenated_string_normalised_to_hex(self):
        col = _UUIDString()
        result = col.process_bind_param("f6e9adfd-404a-43bf-a457-ac4ca4eb3f75", None)
        assert result == "f6e9adfd404a43bfa457ac4ca4eb3f75"

    def test_already_hex_string_unchanged(self):
        col = _UUIDString()
        hex_str = "f6e9adfd404a43bfa457ac4ca4eb3f75"
        assert col.process_bind_param(hex_str, None) == hex_str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source(name: str = "github-main") -> GitSource:
    return GitSource(
        name=name,
        forge_type="github",
        base_url="https://api.github.com",
        auth_type="pat",
        auth_credential=b"encrypted-token-bytes",
    )


def _dest(name: str = "local-store") -> BackupDestination:
    return BackupDestination(
        name=name,
        dest_type="local",
        config=b"encrypted-config-bytes",
    )


def _repo(source_id: UUID, name: str = "org/repo") -> Repository:
    return Repository(
        source_id=source_id,
        repo_name=name,
        clone_url=f"https://github.com/{name}.git",
    )


# ---------------------------------------------------------------------------
# GitSource
# ---------------------------------------------------------------------------


class TestGitSource:
    def test_create_and_persist(self, db_session: Session):
        source = _source()
        db_session.add(source)
        db_session.commit()
        db_session.refresh(source)

        assert isinstance(source.id, UUID)
        assert source.name == "github-main"
        assert source.forge_type == "github"
        assert source.verify_ssl is True

    def test_default_timestamps_set(self, db_session: Session):
        source = _source()
        db_session.add(source)
        db_session.commit()
        db_session.refresh(source)

        assert source.created_at is not None
        assert source.updated_at is not None

    def test_name_unique_constraint(self, db_session: Session):
        db_session.add(_source("dup-source"))
        db_session.commit()

        db_session.add(_source("dup-source"))
        with pytest.raises(IntegrityError):
            db_session.commit()

    def test_query_by_name(self, db_session: Session):
        db_session.add(_source("findme"))
        db_session.commit()

        result = db_session.exec(select(GitSource).where(GitSource.name == "findme")).first()
        assert result is not None
        assert result.forge_type == "github"


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class TestRepository:
    def test_create_and_persist(self, db_session: Session):
        source = _source()
        db_session.add(source)
        db_session.commit()
        db_session.refresh(source)

        repo = _repo(source.id)
        db_session.add(repo)
        db_session.commit()
        db_session.refresh(repo)

        assert isinstance(repo.id, UUID)
        assert repo.is_archived is False
        assert repo.default_branch == "main"

    def test_unique_constraint_source_repo(self, db_session: Session):
        source = _source()
        db_session.add(source)
        db_session.commit()
        db_session.refresh(source)

        db_session.add(_repo(source.id, "org/dupe"))
        db_session.commit()

        db_session.add(_repo(source.id, "org/dupe"))
        with pytest.raises(IntegrityError):
            db_session.commit()

    def test_same_repo_name_different_source_is_allowed(self, db_engine):
        with Session(db_engine) as s:
            src1 = _source("src-1")
            src2 = _source("src-2")
            s.add(src1)
            s.add(src2)
            s.commit()
            s.refresh(src1)
            s.refresh(src2)

            s.add(_repo(src1.id, "org/shared-name"))
            s.add(_repo(src2.id, "org/shared-name"))
            s.commit()  # must not raise

        with Session(db_engine) as s:
            rows = s.exec(select(Repository)).all()
            assert len(rows) == 2

    def test_cascade_delete_removes_repos(self, db_engine):
        # Create source and repos in one session.
        with Session(db_engine) as s:
            source = _source()
            s.add(source)
            s.commit()
            source_id = source.id

            s.add(_repo(source_id, "org/repo-a"))
            s.add(_repo(source_id, "org/repo-b"))
            s.commit()

        # Delete source in a fresh session (no identity-map cache).
        with Session(db_engine) as s:
            src = s.get(GitSource, source_id)
            assert src is not None
            s.delete(src)
            s.commit()

        # Verify repos are gone.
        with Session(db_engine) as s:
            remaining = s.exec(select(Repository)).all()
            assert len(remaining) == 0


# ---------------------------------------------------------------------------
# BackupDestination
# ---------------------------------------------------------------------------


class TestBackupDestination:
    def test_create_and_persist(self, db_session: Session):
        dest = _dest()
        db_session.add(dest)
        db_session.commit()
        db_session.refresh(dest)

        assert isinstance(dest.id, UUID)
        assert dest.dest_type == "local"

    def test_name_unique_constraint(self, db_session: Session):
        db_session.add(_dest("dup-dest"))
        db_session.commit()

        db_session.add(_dest("dup-dest"))
        with pytest.raises(IntegrityError):
            db_session.commit()


# ---------------------------------------------------------------------------
# BackupJob
# ---------------------------------------------------------------------------


class TestBackupJob:
    def _setup(self, db_session: Session) -> tuple[GitSource, BackupDestination]:
        source = _source()
        dest = _dest()
        db_session.add(source)
        db_session.add(dest)
        db_session.commit()
        db_session.refresh(source)
        db_session.refresh(dest)
        return source, dest

    def test_create_and_persist(self, db_session: Session):
        source, dest = self._setup(db_session)

        job = BackupJob(
            name="nightly",
            source_id=source.id,
            destination_id=dest.id,
            schedule_cron="0 2 * * *",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        assert isinstance(job.id, UUID)
        assert job.enabled is True
        assert job.backup_type == "mirror"
        assert job.archive_format == "bundle"
        assert job.retention_count == 0

    def test_defaults(self, db_session: Session):
        source, dest = self._setup(db_session)
        job = BackupJob(name="j", source_id=source.id, destination_id=dest.id)
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        assert job.include_archived is False
        assert job.schedule_cron is None
        assert job.branch_filter is None


# ---------------------------------------------------------------------------
# BackupRun
# ---------------------------------------------------------------------------


class TestBackupRun:
    def _setup(self, db_session: Session) -> tuple[BackupJob, Repository]:
        source = _source()
        dest = _dest()
        db_session.add(source)
        db_session.add(dest)
        db_session.commit()
        db_session.refresh(source)
        db_session.refresh(dest)

        repo = _repo(source.id)
        job = BackupJob(name="j", source_id=source.id, destination_id=dest.id)
        db_session.add(repo)
        db_session.add(job)
        db_session.commit()
        db_session.refresh(repo)
        db_session.refresh(job)
        return job, repo

    def test_create_and_persist(self, db_session: Session):
        job, repo = self._setup(db_session)

        run = BackupRun(job_id=job.id, repo_id=repo.id)
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        assert isinstance(run.id, UUID)
        assert run.status == "pending"
        assert run.trigger == "manual"
        assert run.started_at is None
        assert run.completed_at is None
        assert run.error_message is None
        assert run.duration_secs is None
        assert run.size_bytes is None

    def test_update_to_success(self, db_session: Session):
        from datetime import datetime

        job, repo = self._setup(db_session)

        run = BackupRun(job_id=job.id, repo_id=repo.id)
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        run.status = "success"
        run.started_at = datetime.now(UTC)
        run.completed_at = datetime.now(UTC)
        run.duration_secs = 3.14
        run.size_bytes = 1024 * 1024
        run.checksum_sha256 = "a" * 64
        db_session.commit()
        db_session.refresh(run)

        assert run.status == "success"
        assert run.duration_secs == pytest.approx(3.14)
        assert run.size_bytes == 1_048_576

    def test_update_to_failed(self, db_session: Session):
        job, repo = self._setup(db_session)

        run = BackupRun(job_id=job.id, repo_id=repo.id)
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        run.status = "failed"
        run.error_message = "Connection refused"
        db_session.commit()
        db_session.refresh(run)

        assert run.status == "failed"
        assert run.error_message == "Connection refused"
