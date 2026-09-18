"""Mock EHR data models (own database, own tables).

External IDs let the platform correlate its records with EHR records:
  - patients.external_patient_id      (platform sends PLAT-<id> or its own ref)
  - providers.external_provider_id    (e.g. EHR-PROV-001, matches platform doctors)
  - appointments.source_platform_ref  (platform appointment id — unique, dedupe guard)
  - appointments.external_appointment_id (generated here, returned to the platform)

IdempotencyRecord persists idempotency keys in the DATABASE (never in memory) so
replays survive restarts and concurrent writers are handled by the unique index.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Provider(Base):
    __tablename__ = "ehr_providers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    external_provider_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    specialty: Mapped[str | None] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Patient(Base):
    __tablename__ = "ehr_patients"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    external_patient_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Appointment(Base):
    __tablename__ = "ehr_appointments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # Generated HERE and returned to the platform; the platform stores it as the
    # external reference after a successful/verified synchronization.
    external_appointment_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False, default=lambda: new_id("EHR-")
    )
    patient_id: Mapped[str] = mapped_column(ForeignKey("ehr_patients.id", ondelete="CASCADE"), index=True, nullable=False)
    external_provider_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # Correlation with the platform. Unique: one platform appointment can never
    # create two EHR appointments, even with different idempotency keys.
    source_platform_ref: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    appointment_type: Mapped[str] = mapped_column(String(20), default="in_person")
    status: Mapped[str] = mapped_column(String(20), default="booked", index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    patient: Mapped[Patient] = relationship(lazy="joined")

    @property
    def external_patient_id(self) -> str:
        return self.patient.external_patient_id


APPOINTMENT_STATUSES = {"booked", "cancelled", "completed", "no_show"}


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    # SHA-256 of the canonical request payload — replays with a different body
    # under the same key are rejected instead of silently returning stale data.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
