from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...audit import record_audit
from ...auth import require_hospital_admin
from ...core.validators import validate_phone
from ...database.base import get_db
from ...models import BlockedPeriod, Doctor, DoctorAvailability, Hospital, User
from ...schemas.auth import HospitalOut
from ...schemas.directory import (
    AvailabilityCreate,
    AvailabilityOut,
    BlockedPeriodCreate,
    BlockedPeriodOut,
    DoctorCreate,
    DoctorOut,
    DoctorUpdate,
)

router = APIRouter(prefix="/hospital", tags=["hospital"])


def _get_own_hospital(db: Session, user: User) -> Hospital:
    hospital = db.get(Hospital, user.hospital_id)
    if hospital is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hospital not found")
    return hospital


def _require_approved(hospital: Hospital) -> None:
    if hospital.status != "approved":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Hospital status is '{hospital.status}'; this action requires platform approval",
        )


def _get_own_doctor(db: Session, user: User, doctor_id: str) -> Doctor:
    doctor = db.get(Doctor, doctor_id)
    # Tenant isolation: a doctor from another hospital is simply not found.
    if doctor is None or doctor.hospital_id != user.hospital_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Doctor not found")
    return doctor


# ---------- profile ----------

@router.get("/me", response_model=HospitalOut)
def my_hospital(user: User = Depends(require_hospital_admin), db: Session = Depends(get_db)) -> Hospital:
    return _get_own_hospital(db, user)


@router.put("/me", response_model=HospitalOut)
def update_my_hospital(
    payload: dict, user: User = Depends(require_hospital_admin), db: Session = Depends(get_db)
) -> Hospital:
    hospital = _get_own_hospital(db, user)
    allowed = {"address", "city", "phone", "departments", "specialties", "operating_hours"}
    unknown = set(payload) - allowed
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Fields not editable: {sorted(unknown)}")
    for field, value in payload.items():
        setattr(hospital, field, value)
    record_audit(
        db, action="hospital.profile_updated", actor_user_id=user.id, actor_role=user.role,
        hospital_id=hospital.id, resource_type="hospital", resource_id=hospital.id,
        detail={"fields": sorted(payload.keys())},
    )
    db.commit()
    db.refresh(hospital)
    return hospital


# ---------- doctors ----------

@router.get("/doctors", response_model=list[DoctorOut])
def list_doctors(user: User = Depends(require_hospital_admin), db: Session = Depends(get_db)) -> list[Doctor]:
    return list(db.scalars(select(Doctor).where(Doctor.hospital_id == user.hospital_id)))


@router.post("/doctors", response_model=DoctorOut, status_code=201)
def create_doctor(payload: DoctorCreate, user: User = Depends(require_hospital_admin), db: Session = Depends(get_db)) -> Doctor:
    hospital = _get_own_hospital(db, user)
    _require_approved(hospital)
    doctor = Doctor(hospital_id=hospital.id, **payload.model_dump())
    db.add(doctor)
    db.flush()
    record_audit(
        db, action="doctor.created", actor_user_id=user.id, actor_role=user.role,
        hospital_id=hospital.id, resource_type="doctor", resource_id=doctor.id,
        detail={"specialty": doctor.specialty, "status": doctor.status},
    )
    db.commit()
    db.refresh(doctor)
    return doctor


@router.patch("/doctors/{doctor_id}", response_model=DoctorOut)
def update_doctor(
    doctor_id: str, payload: DoctorUpdate, user: User = Depends(require_hospital_admin), db: Session = Depends(get_db)
) -> Doctor:
    doctor = _get_own_doctor(db, user, doctor_id)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(doctor, field, value)
    record_audit(
        db, action="doctor.updated", actor_user_id=user.id, actor_role=user.role,
        hospital_id=user.hospital_id, resource_type="doctor", resource_id=doctor.id,
        detail={"fields": sorted(data.keys())},
    )
    db.commit()
    db.refresh(doctor)
    return doctor


# ---------- availability ----------

@router.get("/doctors/{doctor_id}/availability", response_model=list[AvailabilityOut])
def list_availability(doctor_id: str, user: User = Depends(require_hospital_admin), db: Session = Depends(get_db)) -> list[DoctorAvailability]:
    _get_own_doctor(db, user, doctor_id)
    return list(db.scalars(select(DoctorAvailability).where(DoctorAvailability.doctor_id == doctor_id)))


@router.post("/doctors/{doctor_id}/availability", response_model=AvailabilityOut, status_code=201)
def create_availability(
    doctor_id: str, payload: AvailabilityCreate, user: User = Depends(require_hospital_admin), db: Session = Depends(get_db)
) -> DoctorAvailability:
    doctor = _get_own_doctor(db, user, doctor_id)
    if payload.start_time >= payload.end_time:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start_time must be before end_time")
    row = DoctorAvailability(
        hospital_id=user.hospital_id,
        doctor_id=doctor.id,
        weekday=payload.weekday,
        start_time=payload.start_time,
        end_time=payload.end_time,
        slot_minutes=payload.slot_minutes,
        appointment_types=payload.appointment_types,
        is_active=payload.is_active,
    )
    db.add(row)
    db.flush()
    record_audit(
        db, action="availability.created", actor_user_id=user.id, actor_role=user.role,
        hospital_id=user.hospital_id, resource_type="doctor_availability", resource_id=row.id,
        detail={"doctor_id": doctor.id, "weekday": row.weekday, "start": row.start_time, "end": row.end_time},
    )
    db.commit()
    db.refresh(row)
    return row


@router.delete("/doctors/{doctor_id}/availability/{availability_id}", status_code=204)
def delete_availability(
    doctor_id: str, availability_id: str, user: User = Depends(require_hospital_admin), db: Session = Depends(get_db)
) -> None:
    _get_own_doctor(db, user, doctor_id)
    row = db.get(DoctorAvailability, availability_id)
    if row is None or row.doctor_id != doctor_id or row.hospital_id != user.hospital_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Availability not found")
    db.delete(row)
    record_audit(
        db, action="availability.deleted", actor_user_id=user.id, actor_role=user.role,
        hospital_id=user.hospital_id, resource_type="doctor_availability", resource_id=availability_id,
    )
    db.commit()


# ---------- blocked periods / leave ----------

@router.get("/doctors/{doctor_id}/blocked", response_model=list[BlockedPeriodOut])
def list_blocked(doctor_id: str, user: User = Depends(require_hospital_admin), db: Session = Depends(get_db)) -> list[BlockedPeriod]:
    _get_own_doctor(db, user, doctor_id)
    return list(db.scalars(select(BlockedPeriod).where(BlockedPeriod.doctor_id == doctor_id)))


@router.post("/doctors/{doctor_id}/blocked", response_model=BlockedPeriodOut, status_code=201)
def create_blocked(
    doctor_id: str, payload: BlockedPeriodCreate, user: User = Depends(require_hospital_admin), db: Session = Depends(get_db)
) -> BlockedPeriod:
    _get_own_doctor(db, user, doctor_id)
    try:
        start = datetime.fromisoformat(payload.start_at.replace("Z", "+00:00")).replace(tzinfo=None)
        end = datetime.fromisoformat(payload.end_at.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start_at/end_at must be ISO datetimes") from exc
    if start >= end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start_at must be before end_at")
    row = BlockedPeriod(
        hospital_id=user.hospital_id,
        doctor_id=doctor_id,
        kind=payload.kind,
        reason=payload.reason,
        start_at=start,
        end_at=end,
        created_by_user_id=user.id,
    )
    db.add(row)
    db.flush()
    record_audit(
        db, action=f"blocked.{payload.kind}_created", actor_user_id=user.id, actor_role=user.role,
        hospital_id=user.hospital_id, resource_type="blocked_period", resource_id=row.id,
        detail={"doctor_id": doctor_id},
    )
    db.commit()
    db.refresh(row)
    return row


@router.delete("/doctors/{doctor_id}/blocked/{blocked_id}", status_code=204)
def delete_blocked(
    doctor_id: str, blocked_id: str, user: User = Depends(require_hospital_admin), db: Session = Depends(get_db)
) -> None:
    _get_own_doctor(db, user, doctor_id)
    row = db.get(BlockedPeriod, blocked_id)
    if row is None or row.doctor_id != doctor_id or row.hospital_id != user.hospital_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Blocked period not found")
    db.delete(row)
    db.commit()
