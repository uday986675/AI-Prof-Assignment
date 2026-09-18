"""SQLAlchemy engine/session wiring for the platform database.

SQLite is the default (zero setup). DATABASE_URL can point at PostgreSQL.
`reset_engine` exists so tests can swap in a throwaway database.
"""
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..core.config import settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if ":memory:" in url:
            kwargs["poolclass"] = None
    return create_engine(url, **kwargs)


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


if engine.url.get_backend_name() == "sqlite":
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def ensure_sqlite_dir() -> None:
    """Create ./data directory for SQLite files before first connect."""
    url = settings.database_url
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def reset_engine(url: str) -> None:
    """Swap the global engine/session (used by the test suite)."""
    global engine, SessionLocal  # noqa: PLW0603
    engine = _make_engine(url)
    if engine.url.get_backend_name() == "sqlite":
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
