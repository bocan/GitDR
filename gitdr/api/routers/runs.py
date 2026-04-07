"""API routes for backup runs."""

import json
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from cryptography.fernet import Fernet
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from gitdr.api.deps import get_fernet, get_session
from gitdr.api.schemas import BackupRunRead, RestoreRequest, RestoreResponse, RestoreRunRead
from gitdr.config import get_settings
from gitdr.database.models import BackupDestination, BackupJob, BackupRun, RestoreRun
from gitdr.services import restore as restore_svc
from gitdr.services.storage import build_storage_backend

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("/", response_model=list[BackupRunRead])
def list_runs(
    job_id: UUID | None = None,
    repo_id: UUID | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    session: Session = Depends(get_session),
) -> list[BackupRun]:
    query = select(BackupRun).order_by(col(BackupRun.created_at).desc())
    if job_id is not None:
        query = query.where(BackupRun.job_id == job_id)
    if repo_id is not None:
        query = query.where(BackupRun.repo_id == repo_id)
    if status is not None:
        query = query.where(BackupRun.status == status)
    query = query.offset(offset).limit(limit)
    return list(session.exec(query).all())


@router.get("/{run_id}", response_model=BackupRunRead)
def get_run(run_id: UUID, session: Session = Depends(get_session)) -> BackupRun:
    run = session.get(BackupRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/{run_id}/restores", response_model=list[RestoreRunRead])
def list_restore_runs(
    run_id: UUID,
    session: Session = Depends(get_session),
) -> list[RestoreRun]:
    """List all restore attempts made against a backup run, newest first."""
    run = session.get(BackupRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    query = (
        select(RestoreRun)
        .where(RestoreRun.backup_run_id == run_id)
        .order_by(col(RestoreRun.created_at).desc())
    )
    return list(session.exec(query).all())


@router.get("/{run_id}/restores/{restore_run_id}", response_model=RestoreRunRead)
def get_restore_run(
    run_id: UUID,
    restore_run_id: UUID,
    session: Session = Depends(get_session),
) -> RestoreRun:
    """Get a single restore run record by ID."""
    rr = session.get(RestoreRun, restore_run_id)
    if rr is None:
        raise HTTPException(status_code=404, detail="Restore run not found")
    # backup_run_id may come back as a hex string (no hyphens) from _UUIDString;
    # normalise both sides for comparison.
    stored_hex = str(rr.backup_run_id).replace("-", "")
    if stored_hex != run_id.hex:
        raise HTTPException(status_code=404, detail="Restore run not found")
    return rr


@router.post("/{run_id}/restore", response_model=RestoreResponse, status_code=202)
async def initiate_restore(
    run_id: UUID,
    body: RestoreRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    session: Session = Depends(get_session),
    fernet: Fernet = Depends(get_fernet),
) -> RestoreResponse:
    """
    Initiate a restore from this backup run.

    Creates a ``RestoreRun`` tracking record immediately (202 Accepted) and
    runs the actual restore as a background task.  Poll
    ``GET /runs/{run_id}/restores/{restore_run_id}`` to check status and
    retrieve the log output.

    If ``push_url`` is provided, the restored repo is pushed to that remote
    after reconstitution and the local copy is removed.
    """
    run = session.get(BackupRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "success":
        raise HTTPException(status_code=409, detail="Only successful runs can be restored")
    if not run.archive_path:
        raise HTTPException(status_code=409, detail="Run has no archive path")

    # Resolve the storage backend from the job's destination
    job = session.get(BackupJob, run.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Parent job not found")
    dest = session.get(BackupDestination, job.destination_id)
    if not dest:
        raise HTTPException(status_code=404, detail="Destination not found")

    config = json.loads(fernet.decrypt(dest.config).decode())
    storage = build_storage_backend(dest.dest_type, config)

    settings = get_settings()
    restore_base = settings.gitdr_temp_dir / "restores"

    # Create the RestoreRun tracking record immediately so the caller has an
    # ID to poll.
    restore_run = RestoreRun(
        id=uuid4(),
        backup_run_id=run_id,
        status="pending",
        push_url=body.push_url,
        created_at=datetime.now(UTC),
    )
    session.add(restore_run)
    session.commit()
    session.refresh(restore_run)
    restore_run_id = restore_run.id

    # Obtain the engine so the background task can open its own session after
    # the request session closes.
    engine: Engine = request.app.state.engine

    async def _do_restore() -> None:
        _update_restore_run(engine, restore_run_id, status="running", started_at=datetime.now(UTC))
        try:
            restore_dir, log_output = await restore_svc.run_restore(
                run,
                storage,
                restore_base,
                push_url=body.push_url,
            )
            _update_restore_run(
                engine,
                restore_run_id,
                status="success",
                completed_at=datetime.now(UTC),
                restore_dir=str(restore_dir),
                log_output=log_output,
            )
        except Exception as exc:
            logger.exception("Restore %s failed", restore_run_id)
            _update_restore_run(
                engine,
                restore_run_id,
                status="failed",
                completed_at=datetime.now(UTC),
                error_message=str(exc),
            )

    background_tasks.add_task(_do_restore)

    restore_dir_hint = str(restore_base / str(run_id))
    return RestoreResponse(
        status="accepted",
        restore_run_id=restore_run_id,
        run_id=run_id,
        archive_path=run.archive_path,
        restore_dir=restore_dir_hint,
        push_url=body.push_url,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _update_restore_run(engine: Engine, restore_run_id: UUID, **kwargs: object) -> None:
    """Open a fresh session and patch fields on a RestoreRun record."""
    with Session(engine) as s:
        rr = s.get(RestoreRun, restore_run_id)
        if rr is None:
            logger.warning("RestoreRun %s not found during status update", restore_run_id)
            return
        for key, val in kwargs.items():
            setattr(rr, key, val)
        s.add(rr)
        s.commit()
