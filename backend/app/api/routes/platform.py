from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...audit import record_audit
from ...auth import require_platform_admin
from ...database.base import get_db
from ...models import AuditEvent, Hospital, User
from ...schemas.auth import HospitalOut, HospitalReviewRequest

router = APIRouter(prefix="/platform", tags=["platform-admin"])

_ALLOWED_REVIEW = {"approve", "reject", "suspend", "reactivate"}


@router.get("/hospitals", response_model=list[HospitalOut])
def list_hospitals(user: User = Depends(require_platform_admin), db: Session = Depends(get_db)) -> list[Hospital]:
    return list(db.scalars(select(Hospital).order_by(Hospital.created_at.desc())))


@router.post("/hospitals/{hospital_id}/review", response_model=HospitalOut)
def review_hospital(
    hospital_id: str,
    action: str,
    payload: HospitalReviewRequest | None = None,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> Hospital:
    if action not in _ALLOWED_REVIEW:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"action must be one of {sorted(_ALLOWED_REVIEW)}")
    hospital = db.get(Hospital, hospital_id)
    if hospital is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hospital not found")

    transitions = {
        "approve": {"draft", "submitted", "suspended"},
        "reject": {"draft", "submitted"},
        "suspend": {"approved"},
        "reactivate": {"suspended"},
    }
    if hospital.status not in transitions[action]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot {action} a hospital in status '{hospital.status}'",
        )

    if action == "approve":
        hospital.status = "approved"
        hospital.approved_by_user_id = user.id
        from ...models.user import utcnow

        hospital.approved_at = utcnow()
    elif action == "reject":
        hospital.status = "rejected"
        hospital.rejection_reason = (payload.reason if payload else None) or "Not specified"
    elif action == "suspend":
        hospital.status = "suspended"
    else:
        hospital.status = "approved"

    record_audit(
        db, action=f"hospital.{action}d", actor_user_id=user.id, actor_role=user.role,
        hospital_id=hospital.id, resource_type="hospital", resource_id=hospital.id,
        detail={"reason": hospital.rejection_reason},
    )
    db.commit()
    db.refresh(hospital)
    return hospital


@router.get("/audit", response_model=list[dict])
def recent_audit(
    limit: int = 100, user: User = Depends(require_platform_admin), db: Session = Depends(get_db)
) -> list[dict]:
    rows = db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(min(limit, 500)))
    return [
        {
            "id": r.id,
            "action": r.action,
            "actor_role": r.actor_role,
            "hospital_id": r.hospital_id,
            "resource_type": r.resource_type,
            "resource_id": r.resource_id,
            "created_at": r.created_at.isoformat(),
            "detail": r.detail,
        }
        for r in rows
    ]
