"""
SQLCipher database engine initialisation.

We use sqlcipher3 as the underlying DB-API 2.0 driver and supply a custom
creator function to SQLAlchemy so that every new connection is opened via
sqlcipher3 and the PRAGMA key is set before any SQL is executed.

Connection flow:
  1. sqlcipher3.connect(db_path) opens / creates the encrypted file.
  2. PRAGMA key sets the 32-byte SQLCipher key (passed as hex).
  3. Additional PRAGMAs harden the cipher settings above the SQLCipher defaults.
  4. PRAGMA foreign_keys = ON enforces referential integrity.
  5. A harmless SELECT validates that the key is correct; a wrong passphrase
     causes sqlcipher3 to raise DatabaseError here rather than silently
     returning empty results later.
"""

import logging
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

logger = logging.getLogger(__name__)

# Module-level engine singleton; set by init_engine(), retrieved by get_engine().
_engine: Engine | None = None


def _open_connection(db_path: str, hex_key: str) -> object:
    """
    Open a SQLCipher connection and configure cipher parameters.

    The hex_key must be a 64-character lowercase hex string (32 bytes).
    SQLCipher accepts it via PRAGMA key="x'<hex>'".
    """
    try:
        import sqlcipher3.dbapi2 as _sqlcipher
    except ImportError as exc:
        raise RuntimeError(
            "sqlcipher3 is not installed. "
            "Run 'make setup-sqlcipher-macos' then 'make install-full'."
        ) from exc

    conn = _sqlcipher.connect(db_path, check_same_thread=False)

    # Set encryption key - must be the very first statement.
    conn.execute(f"PRAGMA key=\"x'{hex_key}'\"")

    # Harden cipher configuration beyond defaults.
    # These must match across all connections or the DB becomes unreadable.
    conn.execute("PRAGMA cipher_page_size = 4096")
    conn.execute("PRAGMA kdf_iter = 64000")
    conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512")
    conn.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")

    # Enable foreign key constraint enforcement (SQLite off by default).
    conn.execute("PRAGMA foreign_keys = ON")

    # Validate key: if key is wrong, SQLCipher returns an empty sqlite_master
    # which causes "file is not a database" - catching it here gives a clear error.
    try:
        conn.execute("SELECT count(*) FROM sqlite_master")
    except Exception as exc:
        conn.close()
        raise RuntimeError(
            "Failed to open the database. The master passphrase may be incorrect, "
            "or the database file may be corrupt."
        ) from exc

    return conn


def init_engine(db_path: Path, hex_key: str) -> Engine:
    """
    Create and globally register the SQLAlchemy engine backed by SQLCipher.

    Must be called once at application startup (inside the FastAPI lifespan).
    """
    global _engine

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_str = str(db_path)

    _engine = create_engine(
        # The URL dialect tells SQLAlchemy to use the pysqlite interface;
        # our creator function overrides the actual connection with sqlcipher3.
        "sqlite+pysqlite:///",
        creator=lambda: _open_connection(db_str, hex_key),
    )

    logger.info("SQLCipher database engine initialised at %s", db_str)
    return _engine


def _migrate_schema(engine: Engine) -> None:
    """
    Apply additive schema migrations that create_all cannot handle.

    For each SQLModel table, compare the live PRAGMA table_info against the
    declared columns and issue ALTER TABLE … ADD COLUMN for any that are missing.
    This is safe to run on every startup: it is a no-op when the schema is current.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    for table in SQLModel.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue  # create_all will create it below
        existing = {row["name"] for row in inspector.get_columns(table.name)}
        with engine.begin() as conn:
            for col in table.columns:
                if col.name not in existing:
                    col_type = col.type.compile(engine.dialect)
                    nullable = "" if col.nullable else " NOT NULL"
                    default_arg = getattr(col.default, "arg", None)
                    default = (
                        f" DEFAULT {default_arg!r}"
                        if default_arg is not None and not callable(default_arg)
                        else ""
                    )
                    ddl = (
                        f"ALTER TABLE {table.name} ADD COLUMN"
                        f" {col.name} {col_type}{nullable}{default}"
                    )
                    conn.execute(text(ddl))
                    logger.info("Schema migration: added column %s.%s", table.name, col.name)


def create_tables(engine: Engine) -> None:
    """Create all SQLModel tables if they do not already exist."""
    # Import models here to ensure they are registered with SQLModel.metadata
    import gitdr.database.models  # noqa: F401

    _migrate_schema(engine)
    SQLModel.metadata.create_all(engine)
    logger.info("Database tables verified / created")


def get_engine() -> Engine:
    """Return the module-level engine. Raises if init_engine() has not been called."""
    if _engine is None:
        raise RuntimeError(
            "Database engine is not initialised. "
            "Ensure init_engine() is called during application startup."
        )
    return _engine


def get_session() -> Generator[Session]:
    """
    FastAPI dependency that yields a SQLModel session.

    Usage:
        @app.get("/example")
        def handler(session: Session = Depends(get_session)):
            ...
    """
    with Session(get_engine()) as session:
        yield session
