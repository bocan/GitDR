"""
Unit tests for gitdr.main.

The FastAPI lifespan (which opens the SQLCipher database) is not exercised
here - that lives in integration tests.  These tests cover the parts that
are safe to call without a database: the logging configurator, the health
route handler, the app object itself, and the uvicorn entry point.
"""

import logging
from unittest.mock import MagicMock, patch

from gitdr.api.routers.system import health
from gitdr.main import _configure_logging, app, run

# ---------------------------------------------------------------------------
# _configure_logging
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    def test_sets_debug_level(self):
        _configure_logging("DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_sets_warning_level(self):
        _configure_logging("WARNING")
        assert logging.getLogger().level == logging.WARNING

    def test_sets_error_level(self):
        _configure_logging("ERROR")
        assert logging.getLogger().level == logging.ERROR

    def test_sets_info_level(self):
        _configure_logging("INFO")
        assert logging.getLogger().level == logging.INFO

    def test_sets_critical_level(self):
        _configure_logging("CRITICAL")
        assert logging.getLogger().level == logging.CRITICAL


# ---------------------------------------------------------------------------
# health endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    def test_returns_expected_payload(self):
        result = health()
        assert result.status == "ok"
        assert result.service == "gitdr"

    def test_return_type_has_status(self):
        assert hasattr(health(), "status")

    def test_status_key_is_ok(self):
        assert health().status == "ok"

    def test_service_key_is_gitdr(self):
        assert health().service == "gitdr"


# ---------------------------------------------------------------------------
# FastAPI app object
# ---------------------------------------------------------------------------


class TestAppObject:
    def test_title(self):
        assert app.title == "GitDR"

    def test_version(self):
        assert app.version == "0.1.0"

    def test_openapi_url(self):
        assert app.openapi_url == "/api/openapi.json"

    def test_docs_url(self):
        assert app.docs_url == "/api/docs"


# ---------------------------------------------------------------------------
# run() entry point
# ---------------------------------------------------------------------------


class TestRunEntryPoint:
    def test_calls_uvicorn_run(self):
        mock_uvicorn_run = MagicMock()
        with patch("uvicorn.run", mock_uvicorn_run):
            run()
        mock_uvicorn_run.assert_called_once()

    def test_passes_correct_app_string(self):
        mock_uvicorn_run = MagicMock()
        with patch("uvicorn.run", mock_uvicorn_run):
            run()
        args, _ = mock_uvicorn_run.call_args
        assert args[0] == "gitdr.main:app"

    def test_passes_correct_port(self):
        mock_uvicorn_run = MagicMock()
        with patch("uvicorn.run", mock_uvicorn_run):
            run()
        _, kwargs = mock_uvicorn_run.call_args
        assert kwargs["port"] == 8420

    def test_reload_is_false(self):
        mock_uvicorn_run = MagicMock()
        with patch("uvicorn.run", mock_uvicorn_run):
            run()
        _, kwargs = mock_uvicorn_run.call_args
        assert kwargs["reload"] is False
