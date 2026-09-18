from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..database.base import Base
from .user import new_id, utcnow


class Patient(Base):
    """Patient profile. Data minimization: only fields the platform actually uses."""

    __tablename__ = "patients"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    external_patient_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    preferences: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # e.g. {"preferred_time":"evening"}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
