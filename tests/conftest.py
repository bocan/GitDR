"""
Shared pytest fixtures for all test suites.

Database strategy
-----------------
Unit tests use a plain SQLite in-memory engine so they can run without
libsqlcipher installed on the developer machine.  Foreign key enforcement
is switched on via an event listener so that cascade-delete behaviour is
still tested correctly.

Integration tests use a real SQLCipher engine (see tests/integration/).
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

# Import all models so SQLModel.metadata is fully populated before create_all.
import gitdr.database.models  # noqa: F401 # lgtm[py/unused-import] — side-effect import: populates SQLModel.metadata


@pytest.fixture(scope="function")
def db_engine(tmp_path):
    """
    Plain SQLite in-memory engine for unit tests.

    A fresh schema is created for every test function and torn down
    afterwards. foreign_keys = ON is enabled via an event listener so
    that FK constraints and ON DELETE CASCADE behave as in production.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(db_engine):
    """A SQLModel session scoped to a single test function."""
    with Session(db_engine) as session:
        yield session


@pytest.fixture
def sample_passphrase() -> str:
    """A deterministic passphrase for use in unit tests."""
    return "test-passphrase-for-gitdr-unit-tests"


@pytest.fixture
def sample_salt() -> bytes:
    """A deterministic 32-byte salt for use in unit tests."""
    return bytes(range(32))  # 0x00..0x1f, predictable but valid
