"""
Shared fixtures for API integration tests.

The app lifespan calls init_engine() which uses SQLCipher.  Since the dev
environment may not have sqlcipher3 installed, we patch that call to use an
in-memory SQLite engine so the lifespan completes cleanly.  Each test's
`client` fixture then overrides the FastAPI dependency `get_session` with its
own isolated in-memory engine, so individual tests remain hermetic.
"""

import os
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import gitdr.database.models  # noqa: F401  — populate SQLModel.metadata


def _make_sqlite_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture(autouse=True)
def _patch_sqlcipher():
    """
    Replace the SQLCipher engine initialisation with plain SQLite so the
    FastAPI lifespan can start without sqlcipher3 being installed.
    Also ensure the required env vars are present so Settings validates.
    """
    engine = _make_sqlite_engine()
    env_overrides = {}
    if "GITDR_DB_PASSPHRASE" not in os.environ:
        env_overrides["GITDR_DB_PASSPHRASE"] = "testpassphrase"
    with (
        patch("gitdr.main.init_engine", return_value=engine),
        patch("gitdr.main.create_tables"),
        patch.dict(os.environ, env_overrides),
    ):
        yield
    engine.dispose()
