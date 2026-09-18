from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database.base import Base
from .user import new_id, utcnow


class AuditEvent(Base):
    """Append-only audit trail: who did what, to which resource, when, under which correlation id.

    Note: keep sensitive healthcare content out of `detail` (privacy-aware logging).
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_hospital_time", "hospital_id", "created_at"),
        Index("ix_audit_correlation", "correlation_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(30), nullable=True)
    hospital_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. "doctor.created"
    resource_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
