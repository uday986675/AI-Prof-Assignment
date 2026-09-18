"""Scheduling API routes — thin wrappers over SchedulingService.

Endpoints:
    GET   /doctors/{doctor_id}/availability   slot grid (patients: any approved hospital;
                                              hospital admins: own tenant only)
    POST  /appointments                       patient books a slot (revalidated, race-safe)
    GET   /me/appointments                    patient's own appointments
    GET   /hospital/appointments              tenant-scoped appointment list (hospital admin)
    PATCH /appointments/{id}/cancel           cancel (patient own / hospital admin / doctor)

Business rules live in app.scheduling; these handlers only resolve the caller's
scope, parse parameters, and delegate.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.orm import Session

from ...auth import get_current_user, require_patient
from ...database.base import get_db
from ...models import Appointment, Doctor, Patient, User
from ...schemas.scheduling import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentOut,
    AvailabilitySearchOut,
)
from ...scheduling import SchedulingService, now_utc
from ...services import QuestionnaireService

router = APIRouter(tags=["scheduling"])

DEFAULT_SEARCH_DAYS = 7


def _availability_scope(user: User) -> str | None:
    """Which hospital scope may this caller search? None = any approved hospital."""
    if user.role in ("hospital_admin", "doctor"):
        return user.hospital_id  # tenant-scoped
    if user.role in ("patient", "platform_admin"):
        return None
    from fastapi import HTTPException

    raise HTTPException(status_code=403, detail="Insufficient role")


@router.get("/doctors/{doctor_id}/availability", response_model=AvailabilitySearchOut)
def doctor_availability(
    doctor_id: str,
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    appointment_type: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    service = SchedulingService(db)
    today = now_utc().date()
    return service.search_availability(
        doctor_id,
        from_date or today,
        to_date or (today + timedelta(days=DEFAULT_SEARCH_DAYS - 1)),
        hospital_id=_availability_scope(user),
        appointment_type=appointment_type,
    )


@router.post("/appointments", response_model=AppointmentOut, status_code=status.HTTP_201_CREATED)
def create_appointment(
    payload: AppointmentCreate,
    x_correlation_id: str | None = Header(default=None),
    user: User = Depends(require_patient),
    db: Session = Depends(get_db),
) -> Appointment:
    service = SchedulingService(db)
    correlation_id = x_correlation_id or uuid.uuid4().hex
    patient = service.ensure_patient_profile(user)
    appointment = service.book_appointment(
        patient_id=patient.id,
        doctor_id=payload.doctor_id,
        start_at=payload.start_at,
        appointment_type=payload.appointment_type,
        reason=payload.reason,
        actor_user_id=user.id,
        actor_role=user.role,
        correlation_id=correlation_id,
    )
    # Phase 6: auto-assign pre-visit questionnaires right after a successful
    # booking (idempotent; specialty template + hospital standard form).
    try:
        QuestionnaireService(db).assign_for_appointment(appointment.id, actor_user_id=user.id)
    except Exception:  # questionnaire assignment must never fail a booking
        db.rollback()
    return appointment


@router.get("/me/appointments", response_model=list[AppointmentOut])
def my_appointments(
    user: User = Depends(require_patient),
    db: Session = Depends(get_db),
) -> list[Appointment]:
    service = SchedulingService(db)
    patient = service.ensure_patient_profile(user)
    return service.list_appointments(patient_id=patient.id)


@router.get("/hospital/appointments", response_model=list[AppointmentOut])
def hospital_appointments(
    doctor_id: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Appointment]:
    if user.role == "hospital_admin":
        scope_hospital = user.hospital_id
        scope_doctor = doctor_id
    elif user.role == "doctor":
        doctor = db.query(Doctor).filter(Doctor.user_id == user.id).first()
        if doctor is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Doctor profile not found")
        scope_hospital = user.hospital_id
        scope_doctor = doctor.id
    else:
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Hospital admin or doctor required")
    return SchedulingService(db).list_appointments(
        hospital_id=scope_hospital, doctor_id=scope_doctor
    )


@router.patch("/appointments/{appointment_id}/cancel", response_model=AppointmentOut)
def cancel_appointment(
    appointment_id: str,
    payload: AppointmentCancel,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Appointment:
    service = SchedulingService(db)
    kwargs: dict = {}
    if user.role == "patient":
        patient = db.query(Patient).filter(Patient.user_id == user.id).first()
        if patient is not None:
            kwargs["patient_id"] = patient.id
    elif user.role == "hospital_admin":
        kwargs["hospital_id"] = user.hospital_id
    elif user.role == "doctor":
        doctor = db.query(Doctor).filter(Doctor.user_id == user.id).first()
        if doctor is not None:
            kwargs["doctor_id"] = doctor.id
    elif user.role != "platform_admin":
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Insufficient role")
    return service.cancel_appointment(
        appointment_id,
        cancelled_by_user_id=user.id,
        reason=payload.reason,
        **kwargs,
    )
