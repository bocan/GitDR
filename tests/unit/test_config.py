"""
Unit tests for gitdr.config.

Settings instances are constructed by passing keyword arguments directly so
tests are independent of any .env file or environment variables already set
in the test process.  get_settings() is tested separately using the env var
that the Makefile/CI injects.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from gitdr.config import Settings, get_settings


def _s(**overrides) -> Settings:
    """Build a Settings object with required fields filled in plus any overrides."""
    base = {"gitdr_db_passphrase": "a-good-test-passphrase"}
    return Settings(**{**base, **overrides})


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_port(self):
        assert _s().gitdr_port == 8420

    def test_host(self):
        assert _s().gitdr_host == "0.0.0.0"  # noqa: S104

    def test_log_level(self):
        assert _s().gitdr_log_level == "INFO"

    def test_workers(self):
        assert _s().gitdr_workers == 1

    def test_db_path(self):
        assert _s().gitdr_db_path == Path("./data/gitdr.db")

    def test_cache_dir(self):
        assert _s().gitdr_cache_dir == Path("./data/mirror-cache")

    def test_temp_dir(self):
        assert _s().gitdr_temp_dir == Path("./data/tmp")


# ---------------------------------------------------------------------------
# Passphrase validator
# ---------------------------------------------------------------------------


class TestPassphraseValidator:
    def test_valid_passphrase_stored(self):
        assert _s(gitdr_db_passphrase="my-secret").gitdr_db_passphrase == "my-secret"

    def test_empty_string_rejected(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            _s(gitdr_db_passphrase="")

    def test_whitespace_only_rejected(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            _s(gitdr_db_passphrase="   ")

    def test_single_space_rejected(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            _s(gitdr_db_passphrase=" ")

    def test_passphrase_with_spaces_allowed_if_non_empty(self):
        # Leading/trailing space is fine as long as it is not all whitespace
        s = _s(gitdr_db_passphrase=" real passphrase ")
        assert s.gitdr_db_passphrase == " real passphrase "


# ---------------------------------------------------------------------------
# Log-level validator
# ---------------------------------------------------------------------------


class TestLogLevelValidator:
    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    def test_all_valid_levels_accepted(self, level):
        assert _s(gitdr_log_level=level).gitdr_log_level == level

    def test_lowercase_coerced_to_upper(self):
        assert _s(gitdr_log_level="debug").gitdr_log_level == "DEBUG"

    def test_mixed_case_coerced(self):
        assert _s(gitdr_log_level="Warning").gitdr_log_level == "WARNING"

    def test_invalid_level_rejected(self):
        with pytest.raises(ValidationError):
            _s(gitdr_log_level="VERBOSE")

    def test_numeric_string_rejected(self):
        with pytest.raises(ValidationError):
            _s(gitdr_log_level="10")


# ---------------------------------------------------------------------------
# Workers validator
# ---------------------------------------------------------------------------


class TestWorkersValidator:
    def test_one_accepted(self):
        assert _s(gitdr_workers=1).gitdr_workers == 1

    def test_two_rejected(self):
        with pytest.raises(ValidationError, match="must be 1"):
            _s(gitdr_workers=2)

    def test_zero_rejected(self):
        with pytest.raises(ValidationError, match="must be 1"):
            _s(gitdr_workers=0)

    def test_negative_rejected(self):
        with pytest.raises(ValidationError, match="must be 1"):
            _s(gitdr_workers=-1)


# ---------------------------------------------------------------------------
# get_settings() factory
# ---------------------------------------------------------------------------


class TestGetSettings:
    def test_returns_settings_instance(self):
        # GITDR_DB_PASSPHRASE is provided by the test runner (see Makefile).
        assert isinstance(get_settings(), Settings)

    def test_lru_cache_returns_same_object(self):
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2
