"""Shared FastAPI dependencies for GitDR API."""

from collections.abc import Generator
from typing import Any

from cryptography.fernet import Fernet
from fastapi import Request
from sqlmodel import Session

from gitdr.database.connection import get_engine


def get_session() -> Generator[Session]:
    """Yield a SQLModel session, closing it afterwards."""
    with Session(get_engine()) as session:
        yield session


def get_fernet(request: Request) -> Fernet:
    """Return the Fernet instance from app state (set during lifespan startup)."""
    return Fernet(request.app.state.fernet_key)


def get_scheduler(request: Request) -> Any:
    """Return the AsyncScheduler from app state, or None if not started."""
    return getattr(request.app.state, "scheduler", None)
