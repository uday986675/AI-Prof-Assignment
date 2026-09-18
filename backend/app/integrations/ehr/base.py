"""EHRConnector protocol — the single boundary the platform depends on.

Implementations: MockEHRConnector (HTTP → Mock EHR, in mock_ehr.py). A future
connector for a real EHR plugs in here without touching any service, route,
or AI code.

Error taxonomy (deliberately coarse — Phase 4 classifies recovery actions):
  EHRAuthError            → key rejected; config problem, not transient
  EHRValidationError      → the EHR refused the payload; do NOT blind-retry
  EHRServerError          → EHR responded 5xx; state at the EHR is unknown-ish
  EHRUnavailableError     → timeout / network unreachable; outcome UNKNOWN —
                            the request may still have succeeded at the EHR
  EHRUnknownOutcomeError  → explicit "we could not learn what happened"
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from ...core.config import settings


class EHRConnectorError(Exception):
    """Base class for all connector errors."""

    code = "ehr_error"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.status = status


class EHRAuthError(EHRConnectorError):
    code = "ehr_auth_failed"


class EHRValidationError(EHRConnectorError):
    code = "ehr_validation_failed"


class EHRNotFoundError(EHRConnectorError):
    code = "ehr_not_found"


class EHRServerError(EHRConnectorError):
    code = "ehr_server_error"


class EHRUnavailableError(EHRConnectorError):
    code = "ehr_unavailable"

    def __init__(self, message: str = "EHR unreachable (timeout or network error)"):
        super().__init__(message)
        self.outcome = "unknown"


class EHRUnknownOutcomeError(EHRUnavailableError):
    code = "ehr_unknown_outcome"


@dataclass(frozen=True)
class EHRPatient:
    external_patient_id: str
    full_name: str
    date_of_birth: str | None = None
    phone: str | None = None
    email: str | None = None


@dataclass(frozen=True)
class EHRAppointment:
    external_appointment_id: str
    external_patient_id: str
    external_provider_id: str
    source_platform_ref: str | None
    start_at: datetime
    end_at: datetime
    appointment_type: str
    status: str
    reason: str | None = None


@runtime_checkable
class EHRConnector(Protocol):
    def create_patient(self, patient: EHRPatient, *, correlation_id: str | None = None) -> EHRPatient: ...

    def get_patient(self, external_patient_id: str, *, correlation_id: str | None = None) -> EHRPatient: ...

    def create_appointment(
        self,
        appointment: EHRAppointment,
        *,
        idempotency_key: str,
        correlation_id: str | None = None,
    ) -> tuple[EHRAppointment, bool]: ...

    def get_appointment(self, external_appointment_id: str, *, correlation_id: str | None = None) -> EHRAppointment: ...

    def find_appointment_by_platform_ref(self, platform_ref: str, *, correlation_id: str | None = None) -> EHRAppointment | None: ...

    def update_appointment(
        self,
        external_appointment_id: str,
        *,
        status: str | None = None,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> EHRAppointment: ...


def build_ehr_connector() -> EHRConnector:
    """Factory — the only place that knows which connector implementation runs.

    EHR_MODE=mock (default) → MockEHRConnector over HTTP. A real EHR connector
    would be selected here; no other code changes.
    """
    mode = (settings.ehr_mode or "mock").lower()
    if mode == "mock":
        from .mock_ehr import MockEHRConnector

        return MockEHRConnector()
    raise ValueError(f"Unknown EHR_MODE: {mode!r} (supported: 'mock')")
