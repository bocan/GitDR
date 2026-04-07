"""
Unit tests for gitdr.database.connection.

The SQLCipher-dependent paths (init_engine, create_tables, the full
_open_connection happy path) require libsqlcipher and are covered by
integration tests.  These unit tests cover the error paths that can be
exercised without SQLCipher installed.
"""

import sys
from unittest.mock import patch

import pytest

import gitdr.database.connection as conn_module

# ---------------------------------------------------------------------------
# get_engine() - uninitialized sentinel
# ---------------------------------------------------------------------------


class TestGetEngine:
    def test_raises_before_init(self):
        """get_engine() must raise RuntimeError when no engine has been created."""
        original = conn_module._engine
        try:
            conn_module._engine = None
            with pytest.raises(RuntimeError, match="not initialised"):
                conn_module.get_engine()
        finally:
            conn_module._engine = original

    def test_error_message_mentions_init_engine(self):
        original = conn_module._engine
        try:
            conn_module._engine = None
            with pytest.raises(RuntimeError, match="init_engine"):
                conn_module.get_engine()
        finally:
            conn_module._engine = original


# ---------------------------------------------------------------------------
# get_session() - propagates uninitialized error
# ---------------------------------------------------------------------------


class TestGetSession:
    def test_raises_when_no_engine(self):
        """get_session() is a generator; advancing it should raise RuntimeError."""
        original = conn_module._engine
        try:
            conn_module._engine = None
            with pytest.raises(RuntimeError, match="not initialised"):
                next(conn_module.get_session())
        finally:
            conn_module._engine = original


# ---------------------------------------------------------------------------
# _open_connection() - missing sqlcipher3 package
# ---------------------------------------------------------------------------


class TestOpenConnection:
    def test_import_error_gives_clear_runtime_error(self):
        """A missing sqlcipher3 package must produce a helpful RuntimeError."""
        with patch.dict(sys.modules, {"sqlcipher3": None, "sqlcipher3.dbapi2": None}):
            with pytest.raises(RuntimeError, match="sqlcipher3 is not installed"):
                conn_module._open_connection("/fake/db.sqlite", "a" * 64)

    def test_import_error_message_mentions_make_target(self):
        with patch.dict(sys.modules, {"sqlcipher3": None, "sqlcipher3.dbapi2": None}):
            with pytest.raises(RuntimeError, match="setup-sqlcipher-macos"):
                conn_module._open_connection("/fake/db.sqlite", "a" * 64)
