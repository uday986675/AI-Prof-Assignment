"""Mock EHR HTTP API — thin routes over EHRService.

All business endpoints require the X-API-Key header; /health is public.
Fault injection middleware is installed globally but inert unless enabled via
env or the X-EHR-Fault dev header.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .. import schemas
from ..database import get_db
from ..faults import install_fault_middleware
from ..security import require_api_key
from ..services.ehr_service import EHRService, EHRError

router = APIRouter(prefix="/ehr", tags=["ehr"], dependencies=[Depends(require_api_key)])


def _error_response(exc: EHRError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.args[0], "code": exc.code})


@router.get("/providers", response_model=list[schemas.ProviderOut])
def list_providers(db: Session = Depends(get_db)):
    return EHRService(db).list_providers()


@router.post("/patients", response_model=schemas.PatientOut, status_code=201)
def create_patient(data: schemas.PatientCreate, db: Session = Depends(get_db)):
    try:
        return EHRService(db).upsert_patient(data)
    except EHRError as exc:
        return _error_response(exc)


@router.get("/patients/{external_patient_id}", response_model=schemas.PatientOut)
def get_patient(external_patient_id: str, db: Session = Depends(get_db)):
    try:
        return EHRService(db).get_patient(external_patient_id)
    except EHRError as exc:
        return _error_response(exc)


@router.post("/appointments", response_model=schemas.AppointmentOut, status_code=201)
def create_appointment(
    data: schemas.AppointmentCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    key = request.headers.get("Idempotency-Key") or request.query_params.get("idempotency_key")
    if key and len(key) > 80:
        return JSONResponse(status_code=422, content={"detail": "Idempotency-Key too long", "code": "ehr_validation_failed"})
    try:
        appt, replayed = EHRService(db).create_appointment(
            data,
            idempotency_key=key,
            correlation_id=request.headers.get("X-Correlation-ID"),
        )
    except EHRError as exc:
        return _error_response(exc)
    response = JSONResponse(status_code=200 if replayed else 201, content=schemas.AppointmentOut.model_validate(appt).model_dump(mode="json"))
    response.headers["X-EHR-Idempotent-Replay"] = "true" if replayed else "false"
    response.headers["X-EHR-Appointment-ID"] = appt.external_appointment_id
    return response


@router.get("/appointments", response_model=list[schemas.AppointmentOut])
def list_appointments(status: str | None = Query(None), db: Session = Depends(get_db)):
    try:
        return EHRService(db).list_appointments(status=status)
    except EHRError as exc:
        return _error_response(exc)


@router.get("/appointments/by-platform-ref/{platform_ref}", response_model=schemas.AppointmentOut)
def get_appointment_by_platform_ref(platform_ref: str, db: Session = Depends(get_db)):
    appt = EHRService(db).find_by_platform_ref(platform_ref)
    if not appt:
        return JSONResponse(status_code=404, content={"detail": "No EHR appointment for this platform reference", "code": "ehr_not_found"})
    return schemas.AppointmentOut.model_validate(appt)


@router.get("/appointments/{external_appointment_id}", response_model=schemas.AppointmentOut)
def get_appointment(external_appointment_id: str, db: Session = Depends(get_db)):
    try:
        return EHRService(db).get_appointment(external_appointment_id)
    except EHRError as exc:
        return _error_response(exc)


@router.patch("/appointments/{external_appointment_id}", response_model=schemas.AppointmentOut)
def update_appointment(
    external_appointment_id: str,
    data: schemas.AppointmentUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        return EHRService(db).update_appointment(
            external_appointment_id, data, correlation_id=request.headers.get("X-Correlation-ID")
        )
    except EHRError as exc:
        return _error_response(exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the Mock EHR schema on startup (mirrors the platform's behavior)."""
    from ..database import Base, ensure_sqlite_dir, engine

    ensure_sqlite_dir()
    Base.metadata.create_all(engine)
    yield


app = FastAPI(
    title="Mock EHR",
    description="External hospital EHR simulation for the Healthcare AI Access Platform.",
    version="0.1.0",
    lifespan=lifespan,
)
install_fault_middleware(app)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(router)


@app.get("/health")
def health(db: Session = Depends(get_db)):
    from ..config import settings

    return {"service": "mock_ehr", "status": "ok", "environment": settings.environment}
