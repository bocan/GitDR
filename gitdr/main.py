"""FastAPI application entry point."""

import logging
import logging.config
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gitdr.api import pages
from gitdr.api.routers import destinations, jobs, runs, sources, system
from gitdr.config import get_settings
from gitdr.database.connection import create_tables, init_engine
from gitdr.database.encryption import derive_keys, load_or_create_salt

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                }
            },
            "root": {"handlers": ["console"], "level": level},
        }
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    _configure_logging(settings.gitdr_log_level)

    # Resolve the salt file alongside the database
    salt_path = settings.gitdr_db_path.parent / "gitdr.salt"

    # Load (or create on first run) the random salt
    salt = load_or_create_salt(salt_path)

    # Derive the two independent keys from the master passphrase
    db_hex_key, fernet_key = derive_keys(settings.gitdr_db_passphrase, salt)

    # Store the Fernet key in app state so routes/services can access it
    # via request.app.state.fernet_key
    app.state.fernet_key = fernet_key

    # Initialise the SQLCipher engine and ensure all tables exist
    engine = init_engine(settings.gitdr_db_path, db_hex_key)
    app.state.engine = engine
    create_tables(engine)

    # Start APScheduler and re-register cron jobs from the database
    from gitdr.services import scheduler as svc_scheduler

    svc_scheduler.configure(engine, fernet_key, settings)
    aps = svc_scheduler.build_scheduler()
    async with aps:
        await aps.start_in_background()
        app.state.scheduler = aps
        await svc_scheduler.sync_job_schedules(aps, engine)

        logger.info(
            "GitDR %s started on %s:%d",
            app.version,
            settings.gitdr_host,
            settings.gitdr_port,
        )

        yield

    # aps.__aexit__ stops the scheduler automatically
    engine.dispose()
    logger.info("GitDR shutdown complete")


app = FastAPI(
    title="GitDR",
    description="Self-hosted Git repository backup and restore",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.include_router(sources.router, prefix="/api/v1")
app.include_router(destinations.router, prefix="/api/v1")
app.include_router(jobs.router, prefix="/api/v1")
app.include_router(runs.router, prefix="/api/v1")
app.include_router(system.router, prefix="/api/v1/system")
app.include_router(system.router_repos, prefix="/api/v1")
app.include_router(pages.router)


def run() -> None:
    """Entry point for the `gitdr` CLI command."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "gitdr.main:app",
        host=settings.gitdr_host,
        port=settings.gitdr_port,
        workers=settings.gitdr_workers,
        log_level=settings.gitdr_log_level.lower(),
        # Reload only in development; not safe in production
        reload=False,
    )
