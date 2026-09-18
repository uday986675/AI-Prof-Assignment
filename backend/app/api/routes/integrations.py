"""Integration API routes — appointment EHR synchronization and observability.

Endpoints:
    POST /appointments/{id}/sync-ehr        push the appointment to the Mock EHR
    POST /appointments/{id}/reconcile-ehr   recover an unknown EHR outcome (Phase 4)
    GET  /appointments/{id}/ehr-status      verify the appointment's EHR-side state
    POST /integrations/reconcile-unknown    sweep all unknowns (tenant-scoped)
    GET  /integrations/operations           IntegrationOperation log (tenant-scoped)

Authorization: the owning patient, the appointment's hospital (admin/doctor),
or a platform admin. Routes are thin: all logic is in EHRSyncService and
EHRRecoveryService. Sweep is admin-only because it fans out across the tenant.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database.base import get_db
from ...models import Appointment, IntegrationOperation, Patient, User
from ...schemas.integration import EHRReconcileOut, EHRStatusOut, EHRSyncOut, IntegrationOperationOut
from ...services import EHRRecoveryService, EHRSyncError, EHRSyncService

router = APIRouter(tags=["integrations"])

_ERROR_STATUS = {
    "appointment_not_found": 404,
    "not_synced": 404,
    "already_synced": 409,
    "appointment_cancelled": 409,
    "doctor_missing_external_provider_id": 422,
}


def _authorized_appointment(db: Session, user: User, appointment_id: str) -> Appointment:
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise HTTPException(404, "Appointment not found")
    if user.role == "platform_admin":
        return appt
    if user.hospital_id and appt.hospital_id == user.hospital_id and user.role in ("hospital_admin", "doctor"):
        return appt
    if user.role == "patient":
        patient = db.query(Patient).filter(Patient.user_id == user.id).first()
        if patient is not None and appt.patient_id == patient.id:
            return appt
    raise HTTPException(403, "Not allowed to access this appointment's EHR data")


def _map_sync_error(exc: EHRSyncError) -> HTTPException:
    return HTTPException(_ERROR_STATUS.get(exc.reason, 400), exc.message)


@router.post("/appointments/{appointment_id}/sync-ehr", response_model=EHRSyncOut)
def sync_appointment_to_ehr(
    appointment_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Appointment:
    appt = _authorized_appointment(db, user, appointment_id)
    service = EHRSyncService(db)
    try:
        return service.sync_appointment(appt.id, actor_user_id=user.id)
    except EHRSyncError as exc:
        raise _map_sync_error(exc) from exc


@router.post("/appointments/{appointment_id}/reconcile-ehr", response_model=EHRReconcileOut)
def reconcile_appointment_ehr(
    appointment_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Appointment:
    """Phase 4 recovery: unknown → query EHR by platform ref → adopt or safe-retry."""
    appt = _authorized_appointment(db, user, appointment_id)
    service = EHRRecoveryService(db)
    try:
        return service.reconcile_appointment(appt.id, actor_user_id=user.id)
    except EHRSyncError as exc:
        raise _map_sync_error(exc) from exc


@router.post("/integrations/reconcile-unknown", response_model=list[EHRReconcileOut])
def reconcile_unknown_appointments(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Appointment]:
    """Sweep every unknown-EHR appointment (hospital admins: own tenant only)."""
    if user.role not in ("hospital_admin", "platform_admin"):
        raise HTTPException(403, "Hospital admin or platform admin required")
    service = EHRRecoveryService(db)
    hospital_id = user.hospital_id if user.role == "hospital_admin" else None
    recovered = service.sweep_unknown(hospital_id=hospital_id, actor_user_id=user.id)
    return recovered


@router.get("/appointments/{appointment_id}/ehr-status", response_model=EHRStatusOut)
def appointment_ehr_status(
    appointment_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    appt = _authorized_appointment(db, user, appointment_id)
    service = EHRSyncService(db)
    try:
        return service.get_ehr_appointment(appt.id)
    except EHRSyncError as exc:
        raise _map_sync_error(exc) from exc


@router.get("/integrations/operations", response_model=list[IntegrationOperationOut])
def list_integration_operations(
    appointment_id: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[IntegrationOperation]:
    query = db.query(IntegrationOperation)
    if user.role == "platform_admin":
        pass  # platform admin sees everything
    elif user.role == "hospital_admin":
        tenant_appt_ids = [
            row[0]
            for row in db.query(Appointment.id).filter(Appointment.hospital_id == user.hospital_id).all()
        ]
        query = query.filter(IntegrationOperation.resource_id.in_(tenant_appt_ids))
    else:
        raise HTTPException(403, "Hospital admin or platform admin required")
    if appointment_id:
        query = query.filter(IntegrationOperation.resource_id == appointment_id)
    if correlation_id:
        query = query.filter(IntegrationOperation.correlation_id == correlation_id)
    return list(query.order_by(IntegrationOperation.created_at.desc()).limit(200))
