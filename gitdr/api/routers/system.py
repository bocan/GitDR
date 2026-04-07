"""API routes for system health, statistics, and repositories."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from gitdr.api.deps import get_session
from gitdr.api.schemas import HealthResponse, RepositoryRead, SystemStats
from gitdr.database.models import (
    BackupDestination,
    BackupJob,
    BackupRun,
    GitSource,
    Repository,
)

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="gitdr", version="0.1.0")


@router.get("/stats", response_model=SystemStats)
def get_stats(session: Session = Depends(get_session)) -> SystemStats:
    sources = session.exec(select(GitSource)).all()
    repos = session.exec(select(Repository)).all()
    dests = session.exec(select(BackupDestination)).all()
    jobs = session.exec(select(BackupJob)).all()
    runs = session.exec(select(BackupRun)).all()

    successful = [r for r in runs if r.status == "success"]
    failed = [r for r in runs if r.status == "failed"]
    completed_times: list[datetime] = [r.completed_at for r in runs if r.completed_at is not None]
    last_run_at = max(completed_times) if completed_times else None
    total_size = sum(r.size_bytes for r in runs if r.size_bytes is not None)

    return SystemStats(
        total_sources=len(sources),
        total_repos=len(repos),
        total_destinations=len(dests),
        total_jobs=len(jobs),
        total_runs=len(runs),
        successful_runs=len(successful),
        failed_runs=len(failed),
        last_run_at=last_run_at,
        total_size_bytes=total_size,
    )


# ---------------------------------------------------------------------------
# Repositories (cross-source)
# ---------------------------------------------------------------------------

router_repos = APIRouter(prefix="/repositories", tags=["repositories"])


@router_repos.get("/", response_model=list[RepositoryRead])
def list_repositories(
    source_id: UUID | None = None,
    limit: int = 200,
    session: Session = Depends(get_session),
) -> list[Repository]:
    query = select(Repository).order_by(Repository.repo_name)
    if source_id is not None:
        query = query.where(Repository.source_id == source_id)
    return list(session.exec(query.limit(limit)).all())
