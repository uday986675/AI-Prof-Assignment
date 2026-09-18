from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...auth import get_current_user, require_patient
from ...database.base import get_db
from ...models import Patient, User
from ...schemas.directory import PatientOut, PatientProfileUpdate

router = APIRouter(prefix="/me", tags=["patient"])


def _get_patient(db: Session, user: User) -> Patient:
    patient = db.scalar(select(Patient).where(Patient.user_id == user.id))
    if patient is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Patient profile not found")
    return patient


@router.get("/profile", response_model=PatientOut)
def my_profile(user: User = Depends(require_patient), db: Session = Depends(get_db)) -> Patient:
    return _get_patient(db, user)


@router.patch("/profile", response_model=PatientOut)
def update_profile(
    payload: PatientProfileUpdate, user: User = Depends(require_patient), db: Session = Depends(get_db)
) -> Patient:
    patient = _get_patient(db, user)
    if payload.phone is not None:
        patient.phone = payload.phone
    if payload.date_of_birth is not None:
        try:
            patient.date_of_birth = date.fromisoformat(payload.date_of_birth)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date_of_birth must be YYYY-MM-DD") from exc
    db.commit()
    db.refresh(patient)
    return patient
