from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


class User(Base):
    """Login identity. Role-based access + optional tenant (hospital) membership."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # platform_admin | hospital_admin | doctor | patient
    role: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    hospital_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("hospitals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Hospital(Base):
    """Tenant root. Lifecycle: draft -> submitted -> approved / rejected (or suspended)."""

    __tablename__ = "hospitals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    departments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    specialties: Mapped[list | None] = mapped_column(JSON, nullable=True)
    operating_hours: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # draft | submitted | approved | rejected | suspended
    status: Mapped[str] = mapped_column(String(20), default="submitted", index=True, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
