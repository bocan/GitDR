"""Pydantic schemas for all GitDR API request/response models."""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

# ---------------------------------------------------------------------------
# Git Sources
# ---------------------------------------------------------------------------


class GitSourceBase(BaseModel):
    name: str
    forge_type: str  # github|gitlab|azure_devops|bitbucket|generic
    base_url: str
    auth_type: str  # pat|ssh_key|app_token|oauth
    org_or_group: str | None = None
    verify_ssl: bool = True


class GitSourceCreate(GitSourceBase):
    auth_credential: str  # plaintext; encrypted server-side before storage


class GitSourceUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    auth_type: str | None = None
    auth_credential: str | None = None  # plaintext; encrypted server-side
    org_or_group: str | None = None
    verify_ssl: bool | None = None


class GitSourceRead(GitSourceBase):
    id: UUID
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class ConnectionTestRequest(BaseModel):
    """Test connection without saving — carries the plaintext credential."""

    forge_type: str
    base_url: str
    auth_credential: str
    org_or_group: str | None = None
    verify_ssl: bool = True


class ConnectionTestResponse(BaseModel):
    ok: bool
    message: str


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


class RepositoryRead(BaseModel):
    id: UUID
    source_id: UUID
    repo_name: str
    clone_url: str
    default_branch: str
    description: str | None
    is_archived: bool
    last_seen_at: datetime
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class RepositoryUpdate(BaseModel):
    default_branch: str | None = None


# ---------------------------------------------------------------------------
# Backup Destinations
# ---------------------------------------------------------------------------


class BackupDestinationCreate(BaseModel):
    name: str
    dest_type: str  # local|s3|azure_blob|gcs|sftp
    config: dict[str, Any]  # plaintext dict; JSON-dumped + Fernet-encrypted before storage


class BackupDestinationUpdate(BaseModel):
    name: str | None = None
    config: dict[str, Any] | None = None


class BackupDestinationRead(BaseModel):
    id: UUID
    name: str
    dest_type: str
    # config deliberately omitted — contains encrypted sensitive data
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Backup Jobs
# ---------------------------------------------------------------------------


class BackupJobCreate(BaseModel):
    name: str
    source_id: UUID
    destination_id: UUID
    schedule_cron: str | None = None
    backup_type: str = "mirror"
    branch_filter: list[str] | None = None  # stored as JSON in DB
    included_repos: list[str] | None = None  # explicit repo_name list, null/empty = all
    archive_format: str = "bundle"
    retention_count: int = 0
    include_archived: bool = False
    enabled: bool = True


class BackupJobUpdate(BaseModel):
    name: str | None = None
    schedule_cron: str | None = None
    backup_type: str | None = None
    branch_filter: list[str] | None = None
    included_repos: list[str] | None = None
    archive_format: str | None = None
    retention_count: int | None = None
    include_archived: bool | None = None
    enabled: bool | None = None


class BackupJobRead(BaseModel):
    id: UUID
    name: str
    source_id: UUID
    destination_id: UUID
    schedule_cron: str | None
    backup_type: str
    branch_filter: list[str] | None
    included_repos: list[str] | None
    archive_format: str
    retention_count: int
    include_archived: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

    @field_validator("branch_filter", "included_repos", mode="before")
    @classmethod
    def parse_json_list(cls, v: Any) -> list[str] | None:
        if isinstance(v, str):
            return json.loads(v)  # type: ignore[no-any-return]
        return v  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Backup Runs
# ---------------------------------------------------------------------------


class BackupRunRead(BaseModel):
    id: UUID
    job_id: UUID
    repo_id: UUID
    status: str
    trigger: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_secs: float | None
    size_bytes: int | None
    archive_path: str | None
    ref_manifest: dict[str, str] | None
    checksum_sha256: str | None
    error_message: str | None
    log_output: str | None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

    @field_validator("ref_manifest", mode="before")
    @classmethod
    def parse_ref_manifest(cls, v: Any) -> dict[str, str] | None:
        if isinstance(v, str):
            return json.loads(v)  # type: ignore[no-any-return]
        return v  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class RestoreRequest(BaseModel):
    push_url: str | None = None  # optional new remote to push restored refs to


class RestoreResponse(BaseModel):
    status: str
    restore_run_id: UUID
    run_id: UUID
    archive_path: str
    restore_dir: str
    push_url: str | None


class RestoreRunRead(BaseModel):
    id: UUID
    backup_run_id: UUID
    status: str
    push_url: str | None
    restore_dir: str | None
    log_output: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class SystemStats(BaseModel):
    total_sources: int
    total_repos: int
    total_destinations: int
    total_jobs: int
    total_runs: int
    successful_runs: int
    failed_runs: int
    last_run_at: datetime | None
    total_size_bytes: int
