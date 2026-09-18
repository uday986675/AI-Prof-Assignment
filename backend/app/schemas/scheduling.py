"""Pydantic schemas for slots and appointments."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from ..core.validators import validate_nonempty

APPOINTMENT_TYPES = ("in_person", "video")


class SlotOut(BaseModel):
    start_at: str
    end_at: str
    available: bool
    reason: str | None = None


class AvailabilityDayOut(BaseModel):
    date: str
    weekday: int
    reason: str | None
    slots: list[SlotOut]


class AvailabilitySearchOut(BaseModel):
    doctor: dict
    hospital_approved: bool
    from_date: str
    to_date: str
    days: list[AvailabilityDayOut]


class AppointmentCreate(BaseModel):
    doctor_id: str
    start_at: str  # ISO 8601 datetime (naive UTC or with offset)
    appointment_type: str | None = None
    reason: str | None = None

    @field_validator("appointment_type")
    @classmethod
    def _type_ok(cls, v: str | None) -> str | None:
        if v is not None and v not in APPOINTMENT_TYPES:
            raise ValueError("appointment_type must be in_person or video")
        return v

    @field_validator("reason")
    @classmethod
    def _reason_ok(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return validate_nonempty(v, "reason", max_len=500)


class AppointmentOut(BaseModel):
    id: str
    hospital_id: str
    patient_id: str
    doctor_id: str
    appointment_type: str
    start_at: datetime
    end_at: datetime
    status: str
    reason: str | None
    correlation_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AppointmentCancel(BaseModel):
    reason: str | None = None


class DateRangeParams(BaseModel):
    from_date: date
    to_date: date
