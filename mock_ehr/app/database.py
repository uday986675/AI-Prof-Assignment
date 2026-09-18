"""Mock EHR database — completely separate engine/session from the platform.

MOCK_EHR_DATABASE_URL defaults to sqlite:///./data/mock_ehr.db. The platform
database is never imported or touched here.
"""
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    return create_engine(url, **kwargs)


engine = _make_engine(settings.mock_ehr_database_url)
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
    url = settings.mock_ehr_database_url
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def reset_engine(url: str) -> None:
    """Swap engine/session (used by tests)."""
    global engine, SessionLocal  # noqa: PLW0603
    engine = _make_engine(url)
    if engine.url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
