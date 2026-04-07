"""
APScheduler v4 integration for GitDR.

Uses an in-memory data store (MemoryDataStore) and re-registers all active
cron schedules from the database on each startup.  This avoids the complexity
of wiring APScheduler's SQLAlchemy data store to the same SQLCipher engine.

Module-level state (_engine, _fernet_key, _settings) is set once by
``configure()`` during app lifespan startup so that scheduler callbacks can
open database sessions and decrypt config without needing access to the
FastAPI Request object.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from apscheduler import AsyncScheduler
from apscheduler.datastores.memory import MemoryDataStore
from apscheduler.eventbrokers.local import LocalEventBroker
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session, select

from gitdr.database.models import BackupDestination, BackupJob

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state — populated once at startup by configure()
# ---------------------------------------------------------------------------

_engine: Any = None
_fernet_key: bytes | None = None
_settings: Any = None  # gitdr.config.Settings


def configure(engine: Any, fernet_key: bytes, settings: Any) -> None:
    """
    Wire the scheduler callbacks to the database engine and encryption key.

    Must be called once during app startup before any schedule fires.
    """
    global _engine, _fernet_key, _settings
    _engine = engine
    _fernet_key = fernet_key
    _settings = settings


# ---------------------------------------------------------------------------
# Storage backend factory
# (imported here to avoid circular import; also available from storage/__init__)
# ---------------------------------------------------------------------------


def _build_storage(dest_type: str, config: dict[str, Any]) -> Any:
    """Build a StorageBackend instance from a decrypted config dict."""
    from gitdr.services.storage import build_storage_backend

    return build_storage_backend(dest_type, config)


# ---------------------------------------------------------------------------
# Scheduled callback — invoked by APScheduler
# ---------------------------------------------------------------------------


async def _run_scheduled_backup(job_id: str) -> None:
    """
    APScheduler callback: load job from DB, build storage, run backup.

    Any exception is caught and logged so a single failure does not prevent
    future scheduled runs.
    """
    if _engine is None or _fernet_key is None or _settings is None:
        logger.error("Scheduler not configured; skipping scheduled backup for job %s", job_id)
        return

    from cryptography.fernet import Fernet

    from gitdr.services.backup import run_backup_job

    fernet = Fernet(_fernet_key)

    try:
        with Session(_engine) as session:
            job = session.get(BackupJob, UUID(job_id))
            if job is None:
                logger.warning("Scheduled job %s not found in DB; skipping", job_id)
                return
            if not job.enabled:
                logger.info("Scheduled job %s is disabled; skipping", job_id)
                return

            dest = session.get(BackupDestination, job.destination_id)
            if dest is None:
                logger.error("Destination for scheduled job %s not found; skipping", job_id)
                return

            config = json.loads(fernet.decrypt(dest.config).decode())
            storage = _build_storage(dest.dest_type, config)
            await run_backup_job(job, session, storage, _settings, trigger="scheduled")

        logger.info("Scheduled backup job %s completed", job_id)
    except Exception:
        logger.exception("Scheduled backup job %s failed", job_id)


# ---------------------------------------------------------------------------
# Scheduler factory
# ---------------------------------------------------------------------------


def build_scheduler() -> AsyncScheduler:
    """Create an AsyncScheduler backed by an in-memory data store."""
    return AsyncScheduler(
        data_store=MemoryDataStore(),
        event_broker=LocalEventBroker(),
    )


# ---------------------------------------------------------------------------
# Schedule sync — call once at startup after configure()
# ---------------------------------------------------------------------------


async def sync_job_schedules(scheduler: AsyncScheduler, engine: Any) -> None:
    """
    Re-register all enabled cron jobs from the database with the scheduler.

    Called at startup to restore schedules lost when the process was stopped
    (MemoryDataStore does not persist across restarts).
    """
    with Session(engine) as session:
        jobs = list(
            session.exec(
                select(BackupJob).where(
                    BackupJob.enabled == True,  # noqa: E712
                    BackupJob.schedule_cron.isnot(None),  # type: ignore[union-attr]
                )
            ).all()
        )

    for job in jobs:
        if not job.schedule_cron:
            continue
        try:
            await scheduler.add_schedule(
                _run_scheduled_backup,
                CronTrigger.from_crontab(job.schedule_cron),
                id=str(job.id),
                kwargs={"job_id": str(job.id)},
                misfire_grace_time=3600,
                conflict_policy=__import__(
                    "apscheduler", fromlist=["ConflictPolicy"]
                ).ConflictPolicy.replace,
            )
            logger.info("Registered cron schedule for job '%s' (%s)", job.name, job.schedule_cron)
        except Exception:
            logger.exception("Failed to register schedule for job '%s' (%s)", job.name, job.id)


# ---------------------------------------------------------------------------
# Per-job schedule helpers — called from job CRUD routes
# ---------------------------------------------------------------------------


async def add_job_schedule(scheduler: AsyncScheduler, job: BackupJob) -> None:
    """Register (or replace) the cron schedule for *job*."""
    if not job.schedule_cron:
        return
    from apscheduler import ConflictPolicy

    await scheduler.add_schedule(
        _run_scheduled_backup,
        CronTrigger.from_crontab(job.schedule_cron),
        id=str(job.id),
        kwargs={"job_id": str(job.id)},
        misfire_grace_time=3600,
        conflict_policy=ConflictPolicy.replace,
    )
    logger.info("Registered schedule for job '%s' (%s)", job.name, job.schedule_cron)


async def remove_job_schedule(scheduler: AsyncScheduler, job_id: UUID) -> None:
    """Remove the cron schedule for *job_id* if it exists."""
    from apscheduler import ScheduleLookupError

    try:
        await scheduler.remove_schedule(str(job_id))
    except ScheduleLookupError:
        pass  # schedule was never registered (e.g. job had no cron)


# ---------------------------------------------------------------------------
# Background-task helper for manual job runs
# ---------------------------------------------------------------------------


async def run_job_now(job_id: UUID, engine: Any, fernet_key: bytes) -> None:
    """
    Execute a backup job immediately (called as a FastAPI BackgroundTask).

    Mirrors ``_run_scheduled_backup`` but with explicit engine/key params
    so there is no dependency on module-level state being initialised.
    """
    from cryptography.fernet import Fernet

    from gitdr.services.backup import run_backup_job
    from gitdr.services.storage import build_storage_backend

    fernet = Fernet(fernet_key)
    settings = _settings
    if settings is None:
        from gitdr.config import get_settings

        settings = get_settings()

    try:
        with Session(engine) as session:
            job = session.get(BackupJob, job_id)
            if job is None:
                logger.error("Manual backup: job %s not found", job_id)
                return
            dest = session.get(BackupDestination, job.destination_id)
            if dest is None:
                logger.error("Manual backup: destination not found for job %s", job_id)
                return

            config = json.loads(fernet.decrypt(dest.config).decode())
            storage = build_storage_backend(dest.dest_type, config)
            await run_backup_job(job, session, storage, settings, trigger="manual")

        logger.info("Manual backup job %s completed", job_id)
    except Exception:
        logger.exception("Manual backup job %s failed", job_id)


# ---------------------------------------------------------------------------
# Discovery background-task helper
# ---------------------------------------------------------------------------


async def run_discovery_now(source_id: UUID, engine: Any, fernet_key: bytes) -> None:
    """
    Run forge discovery for *source_id* (called as a FastAPI BackgroundTask).
    """
    from gitdr.services.discovery import run_discovery

    try:
        result = await run_discovery(source_id, engine, fernet_key)
        logger.info("Discovery for source %s completed: %s", source_id, result)
    except Exception:
        logger.exception("Discovery for source %s failed", source_id)
