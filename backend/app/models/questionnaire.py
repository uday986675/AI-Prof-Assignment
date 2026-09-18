"""Pre-visit questionnaires (Phase 6).

Two tables, deliberately minimal:

- ``QuestionnaireTemplate`` — a hospital-owned, reusable question list
  (JSON "questions" column; each question is a plain dict with a stable
  ``key``, a ``kind`` from QUESTION_KINDS, a ``prompt``, and flags).
- ``QuestionnaireAssignment`` — one form instance per (appointment,
  template). It carries the conversationally-collected ``answers`` dict
  (keyed by question key), its lifecycle status, and the timestamps needed
  for the doctor view.

There is intentionally no platform-wide template registry: each hospital
owns its forms (tenant isolation), and appointment assignment rows link to
exactly one template. Cancelling an appointment cancels its pending forms
(QuestionnaireService.handle_cancellation).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database.base import Base
from .user import new_id, utcnow

QUESTION_KINDS = ("free_text", "boolean", "single_choice", "multiple_choice", "scale_1_10")
ASSIGNMENT_STATUSES = ("pending", "completed", "cancelled")

TEMPLATE_KINDS = ("standard", "specialty")


class QuestionnaireTemplate(Base):
    """Reusable pre-visit questionnaire definition (tenant-owned).

    The ``questions`` JSON column holds an ordered list of dicts shaped like::

        {
            "key": "symptoms",            # stable identifier, unique in template
            "kind": "free_text",          # QUESTION_KINDS
            "prompt": "Describe your symptoms",
            "required": True,
            "options": ["a", "b"],        # single_choice / multiple_choice only
        }

    Templates are tenant-scoped (hospital_id). ``kind`` distinguishes the
    generic "standard" form from specialty-specific ones (e.g. orthopedics).
    """

    __tablename__ = "questionnaire_templates"
    __table_args__ = (
        UniqueConstraint("hospital_id", "name", name="uq_template_hospital_name"),
        Index("ix_qtemplate_hospital_kind", "hospital_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("hospitals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="standard", nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    questions: Mapped[list] = mapped_column(JSON, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class QuestionnaireAssignment(Base):
    """One questionnaire instance for one appointment.

    ``answers`` maps question keys to validated values (string / bool /
    number / list). ``conversation_id`` links the assignment to the agent
    session that collected the answers when Phase 6's AI flow is used.
    """

    __tablename__ = "questionnaire_assignments"
    __table_args__ = (
        # One form instance per (appointment, template).
        UniqueConstraint("appointment_id", "template_id", name="uq_assignment_appointment_template"),
        Index("ix_qassign_doctor_status", "doctor_id", "status"),
        Index("ix_qassign_patient", "patient_id"),
        Index("ix_qassign_appointment", "appointment_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hospital_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("hospitals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    appointment_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("appointments.id", ondelete="CASCADE"), index=True, nullable=False
    )
    template_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("questionnaire_templates.id", ondelete="RESTRICT"), nullable=False
    )
    patient_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("patients.id", ondelete="CASCADE"), index=True, nullable=False
    )
    doctor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("doctors.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # pending | completed | cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    answers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assigned_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
