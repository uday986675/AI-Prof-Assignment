"""FastAPI application entrypoint.

Run:  uvicorn backend.app.main:app --reload --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path

import logging

STATIC_DIR = Path(__file__).resolve().parent / "static"

logger = logging.getLogger("uvicorn.error")  # surfaces in uvicorn/Render logs

from .database import base as database_base
from .core.config import settings
from .database.base import Base, engine, ensure_sqlite_dir
from .scheduling import ConflictError, SchedulingError, ValidationError
from . import models  # noqa: F401  (registers all tables)

ensure_sqlite_dir()
Base.metadata.create_all(bind=engine)

# ── Temporary Render debug (remove after verifying) ──────────────
print("GEMINI KEY LOADED:", bool(settings.gemini_api_key))
print("GEMINI KEY LENGTH:", len(settings.gemini_api_key))
print("GEMINI MODEL:", settings.gemini_model)
# ─────────────────────────────────────────────────────────────────


def _ensure_sqlite_columns() -> None:
    """Prototype-grade column migration for SQLite.

    create_all adds missing TABLES but not missing COLUMNS on existing tables
    (e.g. a platform.db created in Phase 1/2 lacks the Phase 3 ehr_* columns).
    Real deployments would use Alembic; this keeps old dev databases usable.
    """
    from sqlalchemy import inspect, text

    if engine.url.get_backend_name() != "sqlite":
        return
    wanted = {
        "appointments": [
            ("ehr_sync_status", "VARCHAR(20)"),
            ("external_ehr_appointment_id", "VARCHAR(64)"),
            ("ehr_idempotency_key", "VARCHAR(80)"),
            ("ehr_synced_at", "DATETIME"),
        ],
        "conversation_events": [
            ("audio_origin", "VARCHAR(10)"),
        ],
    }
    with engine.begin() as conn:
        for table, columns in wanted.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if not existing:
                continue  # table doesn't exist yet; create_all handles it
            for column, ddl in columns:
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


_ensure_sqlite_columns()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup seeding for hosts without shell access (e.g. Render free tier).

    Runs only when SEED_DEMO_ON_BOOT is enabled and only seeds the PLATFORM
    database (never the Mock EHR). backend/scripts/seed_demo.py is idempotent
    — every entity is an existence-checked upsert — so repeated boots, worker
    restarts and even concurrent boots cannot create duplicates.
    """
    if settings.seed_demo_on_boot:
        logger.info("SEED_DEMO_ON_BOOT enabled: seeding demo data (idempotent)")
        from backend.scripts.seed_demo import main as seed_demo_main

        seed_demo_main()
        logger.info("Demo data seeding finished")
    yield


app = FastAPI(
    title="Healthcare AI Access Platform",
    version="0.1.0",
    description="Multi-tenant patient intake, scheduling and pre-visit AI agent (prototype)",
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

from .api.routes import (
    agent,
    auth,
    hospital,
    integrations,
    patient,
    platform,
    questionnaire,
    scheduling,
    voice,
)  # noqa: E402

app.include_router(auth.router)
app.include_router(hospital.router)
app.include_router(patient.router)
app.include_router(platform.router)
app.include_router(scheduling.router)
app.include_router(integrations.router)
app.include_router(agent.router)
app.include_router(questionnaire.router)
app.include_router(voice.router)


@app.get("/voice-demo", include_in_schema=False)
def voice_demo() -> FileResponse:
    """Phase 7 browser demo: microphone -> STT -> agent -> TTS (text fallback)."""
    return FileResponse(STATIC_DIR / "voice-demo.html")


@app.exception_handler(SchedulingError)
async def scheduling_error_handler(request, exc: SchedulingError):  # noqa: ANN001
    """Map scheduling domain errors to precise HTTP responses."""
    if exc.reason.endswith("_not_found"):
        code = 404
    elif isinstance(exc, ValidationError):
        code = 422
    elif isinstance(exc, ConflictError):
        code = 409
    else:
        code = 400
    return JSONResponse(status_code=code, content={"detail": exc.message, "reason": exc.reason})


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "service": "platform-api", "version": app.version}
