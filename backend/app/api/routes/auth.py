from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from ...audit import record_audit
from ...core.security import create_access_token, hash_password, verify_password
from ...database.base import get_db
from ...models import Hospital, Patient, User
from ...schemas.auth import (
    LoginRequest,
    RegisterHospitalAdminRequest,
    RegisterPatientRequest,
    TokenResponse,
    UserOut,
)
from ...core.validators import validate_phone
from ...auth import get_current_user
from sqlalchemy.orm import Session

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register-hospital-admin", response_model=TokenResponse, status_code=201)
def register_hospital_admin(
    payload: RegisterHospitalAdminRequest, db: Session = Depends(get_db)
) -> TokenResponse:
    email = payload.email
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    hospital = Hospital(
        name=payload.hospital_name,
        address=payload.address,
        city=payload.city,
        phone=payload.phone,
        status="submitted",
        departments=[],
        specialties=[],
    )
    db.add(hospital)
    db.flush()

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role="hospital_admin",
        hospital_id=hospital.id,
        phone=validate_phone(payload.phone),
    )
    db.add(user)
    db.flush()

    record_audit(
        db,
        action="hospital.submitted",
        actor_user_id=user.id,
        actor_role="hospital_admin",
        hospital_id=hospital.id,
        resource_type="hospital",
        resource_id=hospital.id,
        detail={"name": hospital.name},
    )
    db.commit()

    return TokenResponse(
        access_token=create_access_token(user_id=user.id, role=user.role, hospital_id=user.hospital_id),
        role=user.role,
        hospital_id=user.hospital_id,
        full_name=user.full_name,
    )


@router.post("/register-patient", response_model=TokenResponse, status_code=201)
def register_patient(payload: RegisterPatientRequest, db: Session = Depends(get_db)) -> TokenResponse:
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role="patient",
        phone=validate_phone(payload.phone),
    )
    db.add(user)
    db.flush()

    dob = None
    if payload.date_of_birth:
        try:
            dob = date.fromisoformat(payload.date_of_birth)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date_of_birth must be YYYY-MM-DD") from exc

    db.add(Patient(user_id=user.id, date_of_birth=dob, phone=user.phone))
    record_audit(
        db,
        action="patient.registered",
        actor_user_id=user.id,
        actor_role="patient",
        resource_type="user",
        resource_id=user.id,
    )
    db.commit()

    return TokenResponse(
        access_token=create_access_token(user_id=user.id, role=user.role),
        role=user.role,
        hospital_id=None,
        full_name=user.full_name,
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == payload.email.strip().lower()))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")

    record_audit(db, action="auth.login", actor_user_id=user.id, actor_role=user.role, hospital_id=user.hospital_id)
    db.commit()
    return TokenResponse(
        access_token=create_access_token(user_id=user.id, role=user.role, hospital_id=user.hospital_id),
        role=user.role,
        hospital_id=user.hospital_id,
        full_name=user.full_name,
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    return user
