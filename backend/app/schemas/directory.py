from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from ..core.validators import validate_nonempty, validate_time_str

APPOINTMENT_TYPES = {"in_person", "video"}
DOCTOR_STATUSES = {"invited", "active", "inactive"}


class DoctorCreate(BaseModel):
    full_name: str
    specialty: str
    qualifications: str | None = None
    experience_years: int | None = Field(default=None, ge=0, le=70)
    languages: list[str] | None = None
    consultation_minutes: int = Field(default=30, ge=10, le=240)
    appointment_types: list[str] = Field(default_factory=lambda: ["in_person"])
    status: str = "active"
    external_provider_id: str | None = None

    @field_validator("full_name", "specialty")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        return validate_nonempty(v, "field")

    @field_validator("status")
    @classmethod
    def _status_ok(cls, v: str) -> str:
        if v not in DOCTOR_STATUSES:
            raise ValueError("status must be invited | active | inactive")
        return v

    @field_validator("appointment_types")
    @classmethod
    def _types_ok(cls, v: list[str]) -> list[str]:
        bad = set(v) - APPOINTMENT_TYPES
        if bad:
            raise ValueError(f"unknown appointment types: {sorted(bad)}")
        return v or ["in_person"]


class DoctorUpdate(BaseModel):
    full_name: str | None = None
    specialty: str | None = None
    qualifications: str | None = None
    experience_years: int | None = Field(default=None, ge=0, le=70)
    languages: list[str] | None = None
    consultation_minutes: int | None = Field(default=None, ge=10, le=240)
    appointment_types: list[str] | None = None
    status: str | None = None
    external_provider_id: str | None = None

    @field_validator("status")
    @classmethod
    def _status_ok(cls, v: str | None) -> str | None:
        if v is not None and v not in DOCTOR_STATUSES:
            raise ValueError("status must be invited | active | inactive")
        return v


class DoctorOut(BaseModel):
    id: str
    hospital_id: str
    full_name: str
    specialty: str
    qualifications: str | None
    experience_years: int | None
    languages: list | None
    consultation_minutes: int
    appointment_types: list | None
    status: str
    external_provider_id: str | None

    model_config = {"from_attributes": True}


class AvailabilityCreate(BaseModel):
    doctor_id: str
    weekday: int = Field(ge=0, le=6, description="0=Monday .. 6=Sunday")
    start_time: str
    end_time: str
    slot_minutes: int = Field(default=30, ge=5, le=240)
    appointment_types: list[str] = Field(default_factory=lambda: ["in_person"])
    is_active: bool = True

    @field_validator("start_time", "end_time")
    @classmethod
    def _time_ok(cls, v: str) -> str:
        return validate_time_str(v)

    @field_validator("appointment_types")
    @classmethod
    def _types_ok(cls, v: list[str]) -> list[str]:
        bad = set(v) - APPOINTMENT_TYPES
        if bad:
            raise ValueError(f"unknown appointment types: {sorted(bad)}")
        return v or ["in_person"]


class AvailabilityOut(BaseModel):
    id: str
    doctor_id: str
    weekday: int
    start_time: str
    end_time: str
    slot_minutes: int
    appointment_types: list | None
    is_active: bool

    model_config = {"from_attributes": True}


class BlockedPeriodCreate(BaseModel):
    doctor_id: str
    kind: str = "blocked"  # blocked | leave
    start_at: str  # ISO datetime
    end_at: str  # ISO datetime
    reason: str | None = None

    @field_validator("kind")
    @classmethod
    def _kind_ok(cls, v: str) -> str:
        if v not in {"blocked", "leave"}:
            raise ValueError("kind must be blocked | leave")
        return v


class BlockedPeriodOut(BaseModel):
    id: str
    doctor_id: str
    kind: str
    reason: str | None
    start_at: datetime
    end_at: datetime

    model_config = {"from_attributes": True}


class PatientOut(BaseModel):
    id: str
    user_id: str
    date_of_birth: date | None
    phone: str | None
    external_patient_id: str | None

    model_config = {"from_attributes": True}


class PatientProfileUpdate(BaseModel):
    phone: str | None = None
    date_of_birth: str | None = None
