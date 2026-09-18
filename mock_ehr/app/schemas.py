"""Pydantic schemas for the Mock EHR API."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

APPOINTMENT_TYPES = {"in_person", "video"}


class PatientCreate(BaseModel):
    external_patient_id: str = Field(min_length=1, max_length=64)
    full_name: str = Field(min_length=1, max_length=120)
    date_of_birth: date | None = None
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=120)

    @field_validator("external_patient_id", "full_name")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v


class PatientOut(BaseModel):
    external_patient_id: str
    full_name: str
    date_of_birth: date | None
    phone: str | None
    email: str | None

    model_config = {"from_attributes": True}


class AppointmentCreate(BaseModel):
    external_patient_id: str = Field(min_length=1, max_length=64)
    external_provider_id: str = Field(min_length=1, max_length=64)
    start_at: str  # ISO 8601 (naive UTC or with offset)
    end_at: str
    appointment_type: str = "in_person"
    status: str = "booked"
    reason: str | None = Field(default=None, max_length=500)
    source_platform_ref: str | None = Field(default=None, max_length=64)

    @field_validator("appointment_type")
    @classmethod
    def _type_ok(cls, v: str) -> str:
        if v not in APPOINTMENT_TYPES:
            raise ValueError("appointment_type must be in_person or video")
        return v

    @field_validator("status")
    @classmethod
    def _status_ok(cls, v: str) -> str:
        if v != "booked":
            raise ValueError("created appointments must start as 'booked'")
        return v


class AppointmentUpdate(BaseModel):
    status: str | None = None
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("status")
    @classmethod
    def _status_ok(cls, v: str | None) -> str | None:
        from .models import APPOINTMENT_STATUSES

        if v is not None and v not in APPOINTMENT_STATUSES:
            raise ValueError(f"status must be one of {sorted(APPOINTMENT_STATUSES)}")
        return v


class AppointmentOut(BaseModel):
    external_appointment_id: str
    external_patient_id: str
    external_provider_id: str
    source_platform_ref: str | None
    start_at: datetime
    end_at: datetime
    appointment_type: str
    status: str
    reason: str | None
    correlation_id: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class IdempotencyRecordOut(BaseModel):
    idempotency_key: str
    operation: str
    request_hash: str
    result_external_id: str
    correlation_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ProviderOut(BaseModel):
    external_provider_id: str
    full_name: str
    specialty: str | None
    is_active: bool

    model_config = {"from_attributes": True}
