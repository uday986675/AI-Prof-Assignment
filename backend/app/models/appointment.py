from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database.base import Base
from .user import new_id, utcnow

APPOINTMENT_TYPES = ("in_person", "video")

# Statuses that occupy a doctor's slot. "pending_external" is included now so the
# unique index already covers the pre-EHR-confirmation state introduced in Phase 4.
SLOT_BLOCKING_STATUSES = ("booked", "pending_external")

# Appointment lifecycle. Only "booked" exists in Phase 2; later phases add states
# without changing this map's shape (terminal states have no outgoing edges).
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "booked": {"cancelled", "completed", "no_show"},
    "cancelled": set(),
    "completed": set(),
    "no_show": set(),
}

# EHR synchronization dimension (ehr_sync_status) — deliberately SEPARATE from
# the appointment lifecycle above (no new lifecycle status was needed: the
# two-column model covers every Phase 4 state without touching Phase 1/2
# semantics). "synced" is terminal: it may only be re-set from itself, and it
# is reached ONLY after the EHR record has been verified.
#   unknown → cancelled is forbidden at the service layer: with the EHR outcome
#   unresolved, cancelling here would silently strand a live EHR appointment.
EHR_SYNC_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"synced", "failed", "unknown"},
    "unknown": {"synced", "failed", "unknown"},
    "failed": {"synced", "failed", "unknown"},
    "synced": {"synced"},
}


def ehr_sync_can_transition(current: str | None, target: str) -> bool:
    return target in EHR_SYNC_TRANSITIONS.get(current, set())


class Appointment(Base):
    """A booking between a patient and a doctor at one hospital (tenant-scoped).

    Double-booking protection is layered:
      1. Application level: SchedulingService.validate_slot() re-checks the slot
         inside the booking transaction.
      2. Database level: partial unique index ux_appointments_active_slot — only
         ONE slot-consuming appointment may exist per (doctor_id, start_at).
         The index makes the concurrent-insert race impossible to win twice.
    """

    __tablename__ = "appointments"
    __table_args__ = (
        Index(
            "ux_appointments_active_slot",
            "doctor_id",
            "start_at",
            unique=True,
            sqlite_where=text("status IN ('booked', 'pending_external')"),
            postgresql_where=text("status IN ('booked', 'pending_external')"),
        ),
        Index("ix_appointments_doctor_start", "doctor_id", "start_at"),
        Index("ix_appointments_hospital_status", "hospital_id", "status"),
        Index("ix_appointments_patient_start", "patient_id", "start_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("hospitals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    patient_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("patients.id", ondelete="CASCADE"), index=True, nullable=False
    )
    doctor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("doctors.id", ondelete="CASCADE"), index=True, nullable=False
    )
    appointment_type: Mapped[str] = mapped_column(String(20), default="in_person", nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # booked | cancelled | completed | no_show (pending_external arrives in Phase 4)
    status: Mapped[str] = mapped_column(String(20), default="booked", index=True, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # --- Mock EHR synchronization state (Phase 3) ---
    # None | synced | failed | unknown. "unknown" means the connector timed out
    # or lost the network before the EHR's outcome was determined; Phase 4's
    # reconciliation resolves it by querying the EHR. The platform NEVER treats
    # an unverified appointment as EHR-confirmed.
    ehr_sync_status: Mapped[str | None] = mapped_column(String(20), index=True, nullable=True)
    external_ehr_appointment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ehr_idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ehr_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cancelled_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    hospital = relationship("Hospital", lazy="raise")
    patient = relationship("Patient", lazy="raise")
    doctor = relationship("Doctor", lazy="raise")
