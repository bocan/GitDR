"""Application configuration via environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # Required
    gitdr_db_passphrase: str

    # Paths
    gitdr_db_path: Path = Path("./data/gitdr.db")
    gitdr_cache_dir: Path = Path("./data/mirror-cache")
    gitdr_temp_dir: Path = Path("./data/tmp")

    # Server
    gitdr_host: str = "0.0.0.0"  # noqa: S104
    gitdr_port: int = 8420
    gitdr_log_level: str = "INFO"
    gitdr_workers: int = 1

    @field_validator("gitdr_db_passphrase")
    @classmethod
    def passphrase_must_not_be_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("GITDR_DB_PASSPHRASE must not be empty")
        return v

    @field_validator("gitdr_log_level")
    @classmethod
    def log_level_must_be_valid(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"GITDR_LOG_LEVEL must be one of {allowed}")
        return upper

    @field_validator("gitdr_workers")
    @classmethod
    def workers_must_be_one(cls, v: int) -> int:
        if v != 1:
            raise ValueError("GITDR_WORKERS must be 1. SQLite does not support concurrent writers.")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
