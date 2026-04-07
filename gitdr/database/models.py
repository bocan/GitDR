"""
SQLModel / SQLAlchemy ORM models for GitDR.

All models use UUID primary keys stored as VARCHAR(36) in SQLite.
Timestamps are UTC-aware datetimes.

Encrypted fields (auth_credential, config) are stored as BLOB (bytes) and
contain Fernet ciphertext; decryption is performed in the service layer -
never inside a model method.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, ForeignKey, String, UniqueConstraint
from sqlalchemy import types as sa_types
from sqlmodel import Field, SQLModel


class _UUIDString(sa_types.TypeDecorator[str]):
    """
    VARCHAR(36) column type that accepts Python UUID objects on write.

    SQLModel's automatic UUID->string coercion only applies to fields defined
    with Field(foreign_key=...).  When we supply a custom sa_column (required
    to attach ON DELETE CASCADE), that coercion is skipped, so SQLite receives
    a raw UUID object it cannot bind.  This decorator calls str() on the way
    in and leaves the value as-is on the way out.
    """

    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        # SQLModel stores UUID primary keys as .hex (32 hex chars, no hyphens).
        # We must use the same format so FK lookups match.
        if value is None:
            return None
        if isinstance(value, UUID):
            return value.hex
        # Accept a hyphenated string too - normalise to no-hyphen hex.
        return str(value).replace("-", "")


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Git Sources - forge / VCS host credentials
# ---------------------------------------------------------------------------


class GitSource(SQLModel, table=True):
    """
    A configured VCS host (GitHub, GitLab, Azure DevOps, Bitbucket, generic).

    auth_credential stores Fernet-encrypted credentials (PAT, SSH key, etc.).
    """

    __tablename__ = "git_sources"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(index=True, unique=True, max_length=255)
    forge_type: str = Field(max_length=50)  # github|gitlab|azure_devops|bitbucket|generic
    base_url: str = Field(max_length=2048)
    auth_type: str = Field(max_length=50)  # pat|ssh_key|app_token|oauth
    auth_credential: bytes  # Fernet-encrypted
    org_or_group: str | None = Field(default=None, max_length=255)
    verify_ssl: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


# ---------------------------------------------------------------------------
# Repositories - discovered / tracked repos
# ---------------------------------------------------------------------------


class Repository(SQLModel, table=True):
    """
    A single repository discovered from a GitSource.

    source_id has an ON DELETE CASCADE FK so that removing a GitSource
    automatically purges its repositories.

    The (source_id, repo_name) pair is unique to prevent duplicates when
    a discovery run is re-run against the same forge.
    """

    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("source_id", "repo_name", name="uq_source_repo"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)

    # Cascade FK defined at the SQLAlchemy column level so ON DELETE CASCADE
    # is reflected in the DDL and enforced when PRAGMA foreign_keys = ON.
    # _UUIDString is used instead of String(36) so that Python UUID objects
    # are coerced to str before SQLite sees them (SQLModel's automatic
    # coercion only applies to Field(foreign_key=...) columns, not custom
    # sa_column definitions).
    source_id: UUID = Field(
        sa_column=Column(
            _UUIDString(),
            ForeignKey("git_sources.id", ondelete="CASCADE"),
            nullable=False,
        )
    )

    repo_name: str = Field(max_length=512, index=True)
    clone_url: str = Field(max_length=2048)
    default_branch: str = Field(default="main", max_length=255)
    description: str | None = Field(default=None)
    is_archived: bool = Field(default=False)
    last_seen_at: datetime = Field(default_factory=_utc_now)
    created_at: datetime = Field(default_factory=_utc_now)


# ---------------------------------------------------------------------------
# Backup Destinations - S3, Azure Blob, GCS, local, SFTP
# ---------------------------------------------------------------------------


class BackupDestination(SQLModel, table=True):
    """
    A configured storage backend.

    config stores Fernet-encrypted JSON (bucket name, credentials, region, etc.)
    so no sensitive values are stored in plaintext.
    """

    __tablename__ = "backup_destinations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(unique=True, max_length=255)
    dest_type: str = Field(max_length=50)  # local|s3|azure_blob|gcs|sftp
    config: bytes  # Fernet-encrypted JSON
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


# ---------------------------------------------------------------------------
# Backup Jobs - scheduled or manual backup configurations
# ---------------------------------------------------------------------------


class BackupJob(SQLModel, table=True):
    """
    Ties a GitSource to a BackupDestination with scheduling and filtering options.
    """

    __tablename__ = "backup_jobs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=255)
    source_id: UUID = Field(foreign_key="git_sources.id")
    destination_id: UUID = Field(foreign_key="backup_destinations.id")
    schedule_cron: str | None = Field(default=None, max_length=100)
    backup_type: str = Field(default="mirror", max_length=50)  # mirror|selective
    branch_filter: str | None = Field(default=None)  # JSON array of branch glob patterns
    included_repos: str | None = Field(default=None)  # JSON array of explicit repo_name strings
    archive_format: str = Field(default="bundle", max_length=50)  # bundle|tar_zstd
    retention_count: int = Field(default=0)  # 0 = keep all
    include_archived: bool = Field(default=False)
    enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


# ---------------------------------------------------------------------------
# Backup Runs - individual execution records
# ---------------------------------------------------------------------------


class BackupRun(SQLModel, table=True):
    """
    An immutable record of a single repository backup attempt.

    Populated progressively: status transitions pending -> running -> success|failed.
    """

    __tablename__ = "backup_runs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    job_id: UUID = Field(foreign_key="backup_jobs.id")
    repo_id: UUID = Field(foreign_key="repositories.id")
    status: str = Field(default="pending", max_length=50)  # pending|running|success|failed|skipped
    trigger: str = Field(default="manual", max_length=50)  # scheduled|manual
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)
    duration_secs: float | None = Field(default=None)
    size_bytes: int | None = Field(default=None)
    archive_path: str | None = Field(default=None, max_length=4096)
    ref_manifest: str | None = Field(default=None)  # JSON mapping ref -> sha
    checksum_sha256: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None)
    log_output: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utc_now)


# ---------------------------------------------------------------------------
# Restore Runs - individual restore attempts against a BackupRun
# ---------------------------------------------------------------------------


class RestoreRun(SQLModel, table=True):
    """
    A record of a single restore attempt made against a BackupRun.

    Multiple restore attempts can be made against the same BackupRun.
    Status transitions: pending -> running -> success|failed.
    """

    __tablename__ = "restore_runs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    backup_run_id: UUID = Field(
        sa_column=Column(
            _UUIDString(),
            ForeignKey("backup_runs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    status: str = Field(default="pending", max_length=50)  # pending|running|success|failed
    push_url: str | None = Field(default=None, max_length=2048)
    restore_dir: str | None = Field(default=None, max_length=4096)
    log_output: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utc_now)
