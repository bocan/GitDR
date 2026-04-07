"""API routes for backup jobs."""

import json
import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlmodel import Session, col, select

from gitdr.api.deps import get_scheduler, get_session
from gitdr.api.schemas import BackupJobCreate, BackupJobRead, BackupJobUpdate
from gitdr.database.models import BackupJob, BackupRun

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/", response_model=list[BackupJobRead])
def list_jobs(session: Session = Depends(get_session)) -> list[BackupJob]:
    return list(session.exec(select(BackupJob).order_by(col(BackupJob.created_at).desc())).all())


@router.post("/", response_model=BackupJobRead, status_code=status.HTTP_201_CREATED)
async def create_job(
    data: BackupJobCreate,
    request: Request,
    session: Session = Depends(get_session),
    scheduler: object = Depends(get_scheduler),
) -> BackupJob:
    branch_filter_json = json.dumps(data.branch_filter) if data.branch_filter is not None else None
    included_repos_json = (
        json.dumps(data.included_repos) if data.included_repos is not None else None
    )
    job = BackupJob(
        name=data.name,
        source_id=data.source_id,
        destination_id=data.destination_id,
        schedule_cron=data.schedule_cron,
        backup_type=data.backup_type,
        branch_filter=branch_filter_json,
        included_repos=included_repos_json,
        archive_format=data.archive_format,
        retention_count=data.retention_count,
        include_archived=data.include_archived,
        enabled=data.enabled,
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    if scheduler is not None and job.schedule_cron:
        from apscheduler import AsyncScheduler

        from gitdr.services.scheduler import add_job_schedule

        if isinstance(scheduler, AsyncScheduler):
            await add_job_schedule(scheduler, job)

    return job


@router.get("/{job_id}", response_model=BackupJobRead)
def get_job(job_id: UUID, session: Session = Depends(get_session)) -> BackupJob:
    job = session.get(BackupJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.put("/{job_id}", response_model=BackupJobRead)
async def update_job(
    job_id: UUID,
    data: BackupJobUpdate,
    request: Request,
    session: Session = Depends(get_session),
    scheduler: object = Depends(get_scheduler),
) -> BackupJob:
    job = session.get(BackupJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    updates = data.model_dump(exclude_unset=True)
    if "branch_filter" in updates:
        updates["branch_filter"] = (
            json.dumps(updates["branch_filter"]) if updates["branch_filter"] is not None else None
        )
    if "included_repos" in updates:
        updates["included_repos"] = (
            json.dumps(updates["included_repos"]) if updates["included_repos"] is not None else None
        )

    for key, value in updates.items():
        setattr(job, key, value)
    job.updated_at = datetime.now(UTC)

    session.add(job)
    session.commit()
    session.refresh(job)

    # Sync the schedule: remove old, add new (or just remove if disabled/no cron)
    if scheduler is not None:
        from apscheduler import AsyncScheduler

        from gitdr.services.scheduler import add_job_schedule, remove_job_schedule

        if isinstance(scheduler, AsyncScheduler):
            await remove_job_schedule(scheduler, job.id)
            if job.enabled and job.schedule_cron:
                await add_job_schedule(scheduler, job)

    return job


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(
    job_id: UUID,
    request: Request,
    session: Session = Depends(get_session),
    scheduler: object = Depends(get_scheduler),
) -> None:
    job = session.get(BackupJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if scheduler is not None:
        from apscheduler import AsyncScheduler

        from gitdr.services.scheduler import remove_job_schedule

        if isinstance(scheduler, AsyncScheduler):
            await remove_job_schedule(scheduler, job_id)

    session.delete(job)
    session.commit()


@router.post("/{job_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def trigger_job(
    job_id: UUID,
    background_tasks: BackgroundTasks,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    """Manually trigger a backup job; runs asynchronously in the background."""
    job = session.get(BackupJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.enabled:
        raise HTTPException(status_code=409, detail="Job is disabled")

    from gitdr.services.scheduler import run_job_now

    background_tasks.add_task(
        run_job_now,
        job_id,
        request.app.state.engine,
        request.app.state.fernet_key,
    )
    return {"status": "accepted", "message": f"Backup job '{job.name}' queued for execution"}


@router.get("/{job_id}/runs", response_model=list[BackupJobRead])
def list_job_runs(
    job_id: UUID, limit: int = 50, session: Session = Depends(get_session)
) -> list[BackupRun]:
    job = session.get(BackupJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return list(
        session.exec(
            select(BackupRun)
            .where(BackupRun.job_id == job_id)
            .order_by(col(BackupRun.created_at).desc())
            .limit(limit)
        ).all()
    )
