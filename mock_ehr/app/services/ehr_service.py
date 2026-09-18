"""Mock EHR business service — all write logic, routes stay thin.

Key responsibilities:
  - upsert patients / providers by external ID
  - create appointments idempotently (IdempotencyRecord persisted in DB,
    replayed even after restart; concurrent replays safe via unique index)
  - lookup/update appointments
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import settings


class EHRError(Exception):
    """Base EHR service error."""

    def __init__(self, message: str, code: str = "ehr_error", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class ValidationError(EHRError):
    def __init__(self, message: str, code: str = "ehr_validation_failed"):
        super().__init__(message, code=code, status=422)


class ConflictError(EHRError):
    def __init__(self, message: str, code: str = "ehr_conflict"):
        super().__init__(message, code=code, status=409)


class NotFoundError(EHRError):
    def __init__(self, message: str, code: str = "ehr_not_found"):
        super().__init__(message, code=code, status=404)


def _canonical_payload(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def request_hash_for(payload: dict) -> str:
    return hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()


def _parse_dt(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be an ISO 8601 datetime") from exc


class EHRService:
    def __init__(self, db: Session):
        self.db = db

    # ---------------- patients / providers ----------------

    def upsert_patient(self, data: schemas.PatientCreate) -> models.Patient:
        patient = self.db.scalar(
            select(models.Patient).where(models.Patient.external_patient_id == data.external_patient_id)
        )
        if patient:
            patient.full_name = data.full_name
            patient.date_of_birth = data.date_of_birth
            patient.phone = data.phone
            patient.email = data.email
        else:
            patient = models.Patient(**data.model_dump())
            self.db.add(patient)
        self.db.commit()
        return patient

    def get_patient(self, external_patient_id: str) -> models.Patient:
        patient = self.db.scalar(
            select(models.Patient).where(models.Patient.external_patient_id == external_patient_id)
        )
        if not patient:
            raise NotFoundError(f"Patient {external_patient_id} not found")
        return patient

    def list_providers(self) -> list[models.Provider]:
        return list(self.db.scalars(select(models.Provider).order_by(models.Provider.full_name)))

    # ---------------- appointments ----------------

    def get_appointment(self, external_appointment_id: str) -> models.Appointment:
        appt = self.db.scalar(
            select(models.Appointment).where(models.Appointment.external_appointment_id == external_appointment_id)
        )
        if not appt:
            raise NotFoundError(f"Appointment {external_appointment_id} not found")
        return appt

    def find_by_platform_ref(self, platform_ref: str) -> models.Appointment | None:
        return self.db.scalar(
            select(models.Appointment).where(models.Appointment.source_platform_ref == platform_ref)
        )

    def list_appointments(self, status: str | None = None) -> list[models.Appointment]:
        stmt = select(models.Appointment).order_by(models.Appointment.start_at)
        if status:
            if status not in models.APPOINTMENT_STATUSES:
                raise ValidationError(f"status filter must be one of {sorted(models.APPOINTMENT_STATUSES)}")
            stmt = stmt.where(models.Appointment.status == status)
        return list(self.db.scalars(stmt))

    def create_appointment(
        self,
        data: schemas.AppointmentCreate,
        idempotency_key: str | None,
        correlation_id: str | None = None,
    ) -> tuple[models.Appointment, bool]:
        """Create an appointment, idempotently.

        Returns (appointment, replayed). Raises ConflictError on replay with a
        different payload, or when source_platform_ref already maps elsewhere.
        """
        start = _parse_dt(data.start_at, "start_at")
        end = _parse_dt(data.end_at, "end_at")
        if end <= start:
            raise ValidationError("end_at must be after start_at")

        patient = self.get_patient(data.external_patient_id)  # FK is enforced; explicit 422 is friendlier
        provider_ids = {p.external_provider_id for p in self.list_providers()}
        if data.external_provider_id not in provider_ids:
            raise ValidationError(f"Unknown provider {data.external_provider_id}")

        payload = {
            **data.model_dump(),
            "operation": "create_appointment",
        }

        # --- 1. replay check (committed records) -------------------------------
        if idempotency_key:
            record = self.db.scalar(
                select(models.IdempotencyRecord).where(models.IdempotencyRecord.idempotency_key == idempotency_key)
            )
            if record:
                if record.request_hash != request_hash_for(payload):
                    raise ConflictError("Idempotency-Key reused with a different request payload")
                appt = self.get_appointment(record.result_external_id)
                return appt, True

        # --- 2. dedupe by platform ref -----------------------------------------
        if data.source_platform_ref:
            existing = self.find_by_platform_ref(data.source_platform_ref)
            if existing:
                # Same logical request arriving again (e.g. retried with a new
                # idempotency key): return the existing appointment, do not duplicate.
                return existing, True

        # --- 3. create + persist idempotency atomically -------------------------
        # Generate the external ID up front (not via the column default): the
        # IdempotencyRecord must reference it in the same flush.
        external_appointment_id = models.new_id("EHR-")
        appt = models.Appointment(
            external_appointment_id=external_appointment_id,
            patient_id=patient.id,
            external_provider_id=data.external_provider_id,
            start_at=start,
            end_at=end,
            appointment_type=data.appointment_type,
            status=data.status,
            reason=data.reason,
            source_platform_ref=data.source_platform_ref,
            correlation_id=correlation_id,
        )
        self.db.add(appt)
        if idempotency_key:
            self.db.add(
                models.IdempotencyRecord(
                    idempotency_key=idempotency_key,
                    operation="create_appointment",
                    request_hash=request_hash_for(payload),
                    result_external_id=external_appointment_id,
                    correlation_id=correlation_id,
                )
            )
        try:
            self.db.commit()
        except IntegrityError:
            # Concurrent insert raced us (same idempotency key or same
            # source_platform_ref). Roll back and re-read the winner.
            self.db.rollback()
            if idempotency_key:
                record = self.db.scalar(
                    select(models.IdempotencyRecord).where(models.IdempotencyRecord.idempotency_key == idempotency_key)
                )
                if record:
                    if record.request_hash != request_hash_for(payload):
                        raise ConflictError("Idempotency-Key reused with a different request payload")
                    return self.get_appointment(record.result_external_id), True
            if data.source_platform_ref:
                existing = self.find_by_platform_ref(data.source_platform_ref)
                if existing:
                    return existing, True
            raise ConflictError("Concurrent duplicate appointment creation")
        return appt, False

    def update_appointment(
        self, external_appointment_id: str, data: schemas.AppointmentUpdate, correlation_id: str | None = None
    ) -> models.Appointment:
        appt = self.get_appointment(external_appointment_id)
        if data.status is not None:
            appt.status = data.status
        if data.reason is not None:
            appt.reason = data.reason
        if correlation_id:
            appt.correlation_id = correlation_id
        self.db.commit()
        return appt

    # ---------------- health ----------------

    def health(self) -> dict:
        return {
            "service": "mock_ehr",
            "environment": settings.environment,
            "appointments": self.db.scalar(select(models.Appointment.id).order_by(models.Appointment.id)) is not None,
        }
