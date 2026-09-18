from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from ..core.validators import validate_email, validate_nonempty


class RegisterHospitalAdminRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    full_name: str
    hospital_name: str
    address: str | None = None
    city: str | None = None
    phone: str | None = None

    @field_validator("email")
    @classmethod
    def _email_ok(cls, v: str) -> str:
        return validate_email(v)

    @field_validator("full_name", "hospital_name")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        return validate_nonempty(v, "field")


class RegisterPatientRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    full_name: str
    phone: str | None = None
    date_of_birth: str | None = None  # ISO "YYYY-MM-DD"

    @field_validator("email")
    @classmethod
    def _email_ok(cls, v: str) -> str:
        return validate_email(v)

    @field_validator("full_name")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        return validate_nonempty(v, "full_name")


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    hospital_id: str | None = None
    full_name: str


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    hospital_id: str | None
    phone: str | None
    is_active: bool

    model_config = {"from_attributes": True}


class HospitalOut(BaseModel):
    id: str
    name: str
    address: str | None
    city: str | None
    phone: str | None
    departments: list | None
    specialties: list | None
    operating_hours: dict | None
    status: str
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class HospitalReviewRequest(BaseModel):
    reason: str | None = None
