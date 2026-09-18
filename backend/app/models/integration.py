"""Integration operation log — one row per outbound EHR connector attempt.

Gives the observability requirement a concrete home: every EHR operation is
traceable via correlation_id and records its outcome (success | failed |
unknown). Phase 4's reconciliation will read these rows; nothing retries yet.
"""
from __future__ import annotations

from datetime import datetime

from datetime import timedelta
from typing import ClassVar

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database.base import Base
from .user import new_id, utcnow

# Outcome taxonomy shared with the connector errors:
#   success → the EHR confirmed the operation
#   failed  → the EHR (or our request) definitively rejected it; nothing happened
#   unknown → the connector could not learn the outcome (timeout / network loss)
OPERATION_OUTCOMES = ("success", "failed", "unknown")


class IntegrationOperation(Base):
    __tablename__ = "integration_operations"
    __table_args__ = (
        Index("ix_intops_correlation", "correlation_id"),
        Index("ix_intops_resource", "resource_type", "resource_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    operation: Mapped[str] = mapped_column(String(60), nullable=False)  # ehr.create_appointment, ...
    connector: Mapped[str] = mapped_column(String(40), nullable=False)  # "mock_ehr"
    outcome: Mapped[str] = mapped_column(String(10), nullable=False, default="unknown")
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Platform-side subject of the operation (e.g. "appointment", id=...).
    resource_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Exactly what we sent/kept, for the audit trail (no sensitive clinical text
    # beyond the booking reason the platform itself already stores).
    request_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Strictly monotonic wall clock: integration operations can be written
    # microseconds apart (e.g. a reconciliation safe-retry), and coarse OS
    # clock granularity (notably on Windows) can give identical utcnow()
    # values. A per-process guard guarantees ORDER BY created_at reflects
    # true write order — timestamp ties would make "latest operation"
    # queries non-deterministic.
    _last_created_at: ClassVar[datetime | None] = None

    @staticmethod
    def _next_created_at() -> datetime:
        now = utcnow()
        last = IntegrationOperation._last_created_at
        if last is None or now > last:
            IntegrationOperation._last_created_at = now
            return now
        # Clock has not advanced since the previous op: bump by 1µs.
        IntegrationOperation._last_created_at = last + timedelta(microseconds=1)
        return IntegrationOperation._last_created_at

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_next_created_at, nullable=False)
