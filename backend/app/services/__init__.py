"""Service layer.

EHRSyncService — the platform's ONE controlled path to the Mock EHR.

Flow for a single platform appointment:

    appointment (booked)
        → ensure patient exists in EHR (upsert by external_patient_id)
        → POST /ehr/appointments with Idempotency-Key + platform ref
        → on success: store external_ehr_appointment_id, status=synced
        → on definitive failure: status=failed (IntegrationOperation row logged)
        → on timeout/network loss: status=unknown (Phase 4 reconciles)

Design rules honored here:
  - SchedulingService is untouched (slot engine independent of integration).
  - The connector interface is the only EHR dependency (swap-in for real EHRs).
  - Idempotency: the SAME ehr_idempotency_key is persisted on the appointment
    and reused for every attempt, so a retry can never duplicate the EHR record.

``_suppress_op_log`` is an internal keyword-only flag used by
EHRRecoveryService when it drives a safe retry: the recovery service then
records the single authoritative ``ehr.reconcile_appointment`` operation for
the event, instead of an inner ``ehr.create_appointment`` row that would
ambiguously shadow it in "latest operation" queries. Normal initial
synchronization always logs ``ehr.create_appointment``.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from ..audit import record_audit
from ..integrations.ehr.base import (
    EHRAppointment,
    EHRConnectorError,
    EHRConnector,
    EHRNotFoundError,
    EHRPatient,
    EHRUnavailableError,
    build_ehr_connector,
)
from ..models import (
    Appointment,
    Doctor,
    IntegrationOperation,
    Patient,
    User,
    ehr_sync_can_transition,
)
from .questionnaire import QuestionnaireService  # noqa: F401  (re-exported)

SYNC_STATUSES = ("pending", "synced", "failed", "unknown")

# appointment.status → EHR appointment status vocabulary
_EHR_STATUS_MAP = {"booked": "booked", "cancelled": "cancelled", "completed": "completed", "no_show": "no_show"}


class EHRSyncError(Exception):
    """Definitive, pre-write refusal to synchronize (never an EHR-side failure)."""

    def __init__(self, message: str, *, reason: str, outcome: str | None = None):
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.outcome = outcome


class EHRSyncService:
    connector_name = "mock_ehr"

    def __init__(self, db: Session, connector: EHRConnector | None = None):
        self.db = db
        self.connector = connector or build_ehr_connector()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _get_appointment(self, appointment_id: str) -> Appointment:
        appt = self.db.get(Appointment, appointment_id)
        if appt is None:
            raise EHRSyncError(f"Appointment {appointment_id} not found", reason="appointment_not_found")
        return appt

    def _provider_id_for(self, doctor: Doctor) -> str:
        if not doctor.external_provider_id:
            raise EHRSyncError(
                f"Doctor {doctor.id} has no external_provider_id; configure it before EHR sync",
                reason="doctor_missing_external_provider_id",
            )
        return doctor.external_provider_id

    def _patient_external_id(self, patient: Patient) -> str:
        if patient.external_patient_id:
            return patient.external_patient_id
        external_id = f"PLAT-{patient.id}"
        patient.external_patient_id = external_id
        return external_id

    def _ensure_patient_in_ehr(self, patient: Patient, *, correlation_id: str) -> None:
        user = self.db.get(User, patient.user_id)
        ehr_patient = EHRPatient(
            external_patient_id=self._patient_external_id(patient),
            full_name=user.full_name if user is not None else "Platform Patient",
            date_of_birth=patient.date_of_birth.isoformat() if patient.date_of_birth else None,
            phone=patient.phone,
        )
        self.connector.create_patient(ehr_patient, correlation_id=correlation_id)

    def _set_ehr_status(self, appt: Appointment, target: str) -> None:
        current = appt.ehr_sync_status
        if not ehr_sync_can_transition(current, target):
            raise EHRSyncError(
                f"Cannot move EHR sync status from '{current}' to '{target}'",
                reason="invalid_ehr_sync_transition",
            )
        appt.ehr_sync_status = target

    def _log_operation(
        self,
        *,
        operation: str,
        outcome: str,
        error_code: str | None = None,
        error_detail: str | None = None,
        resource_id: str | None = None,
        request_payload: dict | None = None,
        response_summary: dict | None = None,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        self.db.add(
            IntegrationOperation(
                operation=operation,
                connector=self.connector_name,
                outcome=outcome,
                error_code=error_code,
                error_detail=(error_detail or "")[:500] if error_detail else None,
                resource_type="appointment",
                resource_id=resource_id,
                request_payload=request_payload,
                response_summary=response_summary,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                duration_ms=duration_ms,
            )
        )

    # ------------------------------------------------------------------
    # main entry point
    # ------------------------------------------------------------------

    def sync_appointment(
        self,
        appointment_id: str,
        actor_user_id: str | None = None,
        *,
        _suppress_op_log: bool = False,
    ) -> Appointment:
        """Synchronize one platform appointment to the Mock EHR.

        Returns the updated appointment (ehr_sync_status in SYNC_STATUSES).
        Never raises for EHR-side failures — the outcome is recorded on the
        appointment and in IntegrationOperation; callers read the status.
        """
        appt = self._get_appointment(appointment_id)
        if appt.status == "cancelled":
            raise EHRSyncError("Cannot sync a cancelled appointment", reason="appointment_cancelled")
        if appt.ehr_sync_status == "synced":
            raise EHRSyncError("Appointment already synchronized", reason="already_synced")
        if not appt.correlation_id:
            appt.correlation_id = uuid.uuid4().hex

        doctor = self.db.get(Doctor, appt.doctor_id)
        patient = self.db.get(Patient, appt.patient_id)
        provider_id = self._provider_id_for(doctor)
        correlation_id = appt.correlation_id or uuid.uuid4().hex
        idempotency_key = appt.ehr_idempotency_key or f"plat-appt-{appt.id}"
        appt.ehr_idempotency_key = idempotency_key
        started = datetime.now()

        request_payload = {
            "external_patient_id": self._patient_external_id(patient),
            "external_provider_id": provider_id,
            "start_at": appt.start_at.isoformat(),
            "end_at": appt.end_at.isoformat(),
            "appointment_type": appt.appointment_type,
            "status": _EHR_STATUS_MAP.get(appt.status, "booked"),
            "reason": appt.reason,
            "source_platform_ref": appt.id,
        }
        try:
            self._ensure_patient_in_ehr(patient, correlation_id=correlation_id)
            ehr_appt, replayed = self.connector.create_appointment(
                EHRAppointment(
                    external_appointment_id="",
                    external_patient_id=request_payload["external_patient_id"],
                    external_provider_id=provider_id,
                    source_platform_ref=appt.id,
                    start_at=appt.start_at,
                    end_at=appt.end_at,
                    appointment_type=appt.appointment_type,
                    status=request_payload["status"],
                    reason=appt.reason,
                ),
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
            # VERIFY before claiming success: read the record back from the EHR.
            verified = self.connector.get_appointment(ehr_appt.external_appointment_id, correlation_id=correlation_id)
            if verified.status != request_payload["status"]:
                raise EHRConnectorError(
                    f"EHR verification failed: expected status {request_payload['status']!r}, got {verified.status!r}",
                    code="ehr_verification_failed",
                )
            self._set_ehr_status(appt, "synced")
            appt.external_ehr_appointment_id = ehr_appt.external_appointment_id
            appt.ehr_synced_at = datetime.now()
            outcome = "success"
            error_code = None
            error_detail = None
            response_summary = {
                "external_appointment_id": ehr_appt.external_appointment_id,
                "replayed": replayed,
                "ehr_status": ehr_appt.status,
                "verified": True,
            }
        except EHRConnectorError as exc:
            unknown = isinstance(exc, EHRUnavailableError)
            if unknown:
                self._set_ehr_status(appt, "unknown")
            else:
                self._set_ehr_status(appt, "failed")
            outcome = "unknown" if unknown else "failed"
            error_code = exc.code
            error_detail = exc.message
            response_summary = None
        except Exception as exc:  # unexpected: treat as unknown, surface later
            self._set_ehr_status(appt, "unknown")
            if _suppress_op_log:
                raise
            self._log_operation(
                operation="ehr.create_appointment",
                outcome="unknown",
                error_code="unexpected_error",
                error_detail=str(exc),
                resource_id=appt.id,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                duration_ms=int((datetime.now() - started).total_seconds() * 1000),
                request_payload=request_payload,
                response_summary=None,
            )
            raise

        duration_ms = int((datetime.now() - started).total_seconds() * 1000)
        if not _suppress_op_log:
            self._log_operation(
                operation="ehr.create_appointment",
                outcome=outcome,
                error_code=error_code,
                error_detail=error_detail,
                resource_id=appt.id,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                duration_ms=duration_ms,
                request_payload=request_payload,
                response_summary=response_summary,
            )
        record_audit(
            self.db,
            action=f"ehr.sync_{outcome}",
            actor_user_id=actor_user_id,
            hospital_id=appt.hospital_id,
            resource_type="appointment",
            resource_id=appt.id,
            correlation_id=correlation_id,
            detail={"ehr_appointment_id": appt.external_ehr_appointment_id, "outcome": outcome},
        )
        self.db.commit()
        return appt

    # ------------------------------------------------------------------
    # read-back / observability
    # ------------------------------------------------------------------

    def get_ehr_appointment(self, appointment_id: str) -> dict:
        appt = self._get_appointment(appointment_id)
        if not appt.external_ehr_appointment_id:
            raise EHRSyncError("Appointment has no external EHR reference", reason="not_synced")
        ehr_appt = self.connector.get_appointment(
            appt.external_ehr_appointment_id, correlation_id=appt.correlation_id
        )
        return {
            "appointment_id": appt.id,
            "ehr_sync_status": appt.ehr_sync_status,
            "external_ehr_appointment_id": ehr_appt.external_appointment_id,
            "ehr_status": ehr_appt.status,
            "start_at": ehr_appt.start_at.isoformat(),
            "provider_id": ehr_appt.external_provider_id,
        }

    def list_operations(self, *, appointment_id: str | None = None,
                        correlation_id: str | None = None) -> list[IntegrationOperation]:
        stmt = self.db.query(IntegrationOperation)
        if appointment_id:
            stmt = stmt.filter(IntegrationOperation.resource_id == appointment_id)
        if correlation_id:
            stmt = stmt.filter(IntegrationOperation.correlation_id == correlation_id)
        return list(stmt.order_by(IntegrationOperation.created_at.desc()))


class EHRRecoveryService:
    """Reconciles appointments whose EHR outcome is UNKNOWN.

    Algorithm (spec §6–8):
      1. synced           → return current state (no EHR call at all).
      2. pending/failed   → optionally retry the create with the SAME key.
      3. unknown          → query EHR by platform reference (NEVER blind-retry):
           found   → adopt the external ID, confirm (no second appointment).
           missing → safe retry with the SAME idempotency key + verify.

    Every attempt is idempotent: repeated reconciliation of an already-recovered
    appointment is a no-op, and the EHR's own idempotency record is the last
    line of defense against duplicates.
    """

    def __init__(self, db: Session, connector: EHRConnector | None = None):
        self.db = db
        self.sync_service = EHRSyncService(db, connector=connector)

    @property
    def connector(self) -> EHRConnector:
        return self.sync_service.connector

    def reconcile_appointment(self, appointment_id: str, *, actor_user_id: str | None = None) -> Appointment:
        appt = self.sync_service._get_appointment(appointment_id)
        if appt.status == "cancelled":
            raise EHRSyncError("Cannot reconcile a cancelled appointment", reason="appointment_cancelled")
        if appt.ehr_sync_status == "synced":
            return appt
        if not appt.correlation_id:
            appt.correlation_id = uuid.uuid4().hex

        if appt.ehr_sync_status == "unknown":
            return self._reconcile_unknown(appt, appt.correlation_id, actor_user_id=actor_user_id)

        return self.sync_service.sync_appointment(appt.id, actor_user_id=actor_user_id)

    def _reconcile_unknown(
        self, appt: Appointment, correlation_id: str, *, actor_user_id: str | None = None
    ) -> Appointment:
        started = datetime.now()
        idempotency_key = appt.ehr_idempotency_key or f"plat-appt-{appt.id}"
        appt.ehr_idempotency_key = idempotency_key
        try:
            found = self.connector.find_appointment_by_platform_ref(appt.id, correlation_id=correlation_id)
        except EHRUnavailableError as exc:
            # Can't learn the truth right now: stay unknown, log, return.
            self.sync_service._log_operation(
                operation="ehr.reconcile_appointment",
                outcome="unknown",
                error_code=exc.code,
                error_detail=exc.message,
                resource_id=appt.id,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                duration_ms=int((datetime.now() - started).total_seconds() * 1000),
                request_payload={"platform_ref": appt.id},
                response_summary=None,
            )
            self.db.commit()
            return appt

        if found is None:
            # NOT FOUND: safe to retry the create — with the SAME key.
            expected_status = _EHR_STATUS_MAP.get(appt.status, "booked")
            retried = self.sync_service.sync_appointment(
                appt.id, actor_user_id=actor_user_id, _suppress_op_log=True
            )
            self.sync_service._log_operation(
                operation="ehr.reconcile_appointment",
                outcome="success" if retried.ehr_sync_status == "synced" else retried.ehr_sync_status,
                error_code=None if retried.ehr_sync_status == "synced" else retried.ehr_sync_status,
                error_detail=None,
                resource_id=appt.id,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                duration_ms=int((datetime.now() - started).total_seconds() * 1000),
                request_payload={"platform_ref": appt.id},
                response_summary={
                    "action": "safe_retry",
                    "result_status": retried.ehr_sync_status,
                    "external_appointment_id": retried.external_ehr_appointment_id,
                },
            )
            self.db.commit()
            return retried

        # FOUND: validate the EHR record actually corresponds to this platform
        # appointment before adopting it (never adopt blindly).
        expected_status = _EHR_STATUS_MAP.get(appt.status, "booked")
        if found.status != expected_status:
            self._record_reconcile(
                appt,
                outcome="failed",
                error_code="ehr_reconciliation_mismatch",
                error_detail=(
                    f"EHR record for platform ref has status {found.status!r}, expected {expected_status!r}"
                ),
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                started=started,
                response_summary={"external_appointment_id": found.external_appointment_id, "ehr_status": found.status},
            )
            self.db.commit()
            return appt

        self.sync_service._set_ehr_status(appt, "synced")
        if not appt.external_ehr_appointment_id:
            appt.external_ehr_appointment_id = found.external_appointment_id
        appt.ehr_synced_at = datetime.now()
        self._record_reconcile(
            appt,
            outcome="success",
            error_code=None,
            error_detail=None,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            started=started,
            response_summary={
                "action": "adopted",
                "external_appointment_id": appt.external_ehr_appointment_id,
                "ehr_status": found.status,
            },
        )
        record_audit(
            self.db,
            action="ehr.reconciled_adopted",
            actor_user_id=actor_user_id,
            hospital_id=appt.hospital_id,
            resource_type="appointment",
            resource_id=appt.id,
            correlation_id=correlation_id,
            detail={"external_ehr_appointment_id": appt.external_ehr_appointment_id},
        )
        self.db.commit()
        return appt

    def _record_reconcile(
        self,
        appt: Appointment,
        *,
        outcome: str,
        error_code: str | None,
        error_detail: str | None,
        idempotency_key: str,
        correlation_id: str,
        started: datetime,
        response_summary: dict | None = None,
    ) -> None:
        self.sync_service._log_operation(
            operation="ehr.reconcile_appointment",
            outcome=outcome,
            error_code=error_code,
            error_detail=error_detail,
            resource_id=appt.id,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            duration_ms=int((datetime.now() - started).total_seconds() * 1000),
            request_payload={"platform_ref": appt.id},
            response_summary=response_summary,
        )

    def find_unknown_appointments(self, *, hospital_id: str | None = None) -> list[Appointment]:
        stmt = self.db.query(Appointment).filter(Appointment.ehr_sync_status == "unknown")
        if hospital_id:
            stmt = stmt.filter(Appointment.hospital_id == hospital_id)
        return list(stmt.order_by(Appointment.created_at.asc()))

    def sweep_unknown(self, *, hospital_id: str | None = None, actor_user_id: str | None = None) -> list[Appointment]:
        results: list[Appointment] = []
        for appt in self.find_unknown_appointments(hospital_id=hospital_id):
            try:
                results.append(self.reconcile_appointment(appt.id, actor_user_id=actor_user_id))
            except EHRSyncError:
                continue
        return results
