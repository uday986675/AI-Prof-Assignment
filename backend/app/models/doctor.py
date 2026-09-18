from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database.base import Base
from .user import new_id, utcnow


class Doctor(Base):
    """Doctor profile owned by exactly one hospital (tenant-scoped)."""

    __tablename__ = "doctors"
    __table_args__ = (
        Index("ix_doctors_hospital_specialty", "hospital_id", "specialty"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("hospitals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    specialty: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    qualifications: Mapped[str | None] = mapped_column(String(300), nullable=True)
    experience_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    languages: Mapped[list | None] = mapped_column(JSON, nullable=True)
    consultation_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    appointment_types: Mapped[list | None] = mapped_column(JSON, nullable=True)  # ["in_person","video"]
    # invited | active | inactive
    status: Mapped[str] = mapped_column(String(20), default="active", index=True, nullable=False)
    external_provider_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class DoctorAvailability(Base):
    """Recurring weekly calendar: one row per weekday window.

    Times are 'HH:MM' strings (clinic-local); slot_minutes is the granularity
    used by the scheduling engine to generate bookable slots.
    """

    __tablename__ = "doctor_availability"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_availability_weekday"),
        CheckConstraint("slot_minutes > 0", name="ck_availability_slot_minutes"),
        Index("ix_availability_doctor_weekday", "doctor_id", "weekday"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("hospitals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    doctor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("doctors.id", ondelete="CASCADE"), index=True, nullable=False
    )
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Monday .. 6=Sunday
    start_time: Mapped[str] = mapped_column(String(5), nullable=False)  # "HH:MM"
    end_time: Mapped[str] = mapped_column(String(5), nullable=False)  # "HH:MM"
    slot_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    appointment_types: Mapped[list | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class BlockedPeriod(Base):
    """Ad-hoc blocked time or leave for a doctor. kind: blocked | leave."""

    __tablename__ = "blocked_periods"
    __table_args__ = (
        CheckConstraint("kind IN ('blocked','leave')", name="ck_blocked_kind"),
        Index("ix_blocked_doctor_start", "doctor_id", "start_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("hospitals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    doctor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("doctors.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(10), default="blocked", nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
