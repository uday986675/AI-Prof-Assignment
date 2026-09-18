"""Audit helper: append-only, privacy-aware (never log sensitive clinical content)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import AuditEvent


def record_audit(
    db: Session,
    *,
    action: str,
    actor_user_id: str | None = None,
    actor_role: str | None = None,
    hospital_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    correlation_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Write one audit row. Committing is left to the caller's transaction."""
    db.add(
        AuditEvent(
            action=action,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            hospital_id=hospital_id,
            resource_type=resource_type,
            resource_id=resource_id,
            correlation_id=correlation_id,
            detail=detail or {},
        )
    )
