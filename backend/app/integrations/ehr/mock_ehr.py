"""MockEHRConnector — HTTP implementation of the EHRConnector protocol.

The ONLY module in the platform that knows the Mock EHR's HTTP surface
(X-API-Key header, /ehr/... paths, Idempotency-Key replay semantics).
Everything else depends on EHRConnector.

Deliberately NO retries here: one attempt per call. Retry policy is a Phase 4
workflow concern, not a connector concern. Timeout/network failures raise
EHRUnknownOutcomeError (the request MAY have succeeded — Phase 4 reconciles).
"""
from __future__ import annotations

from datetime import datetime

import httpx

from ...core.config import settings
from .base import (
    EHRAppointment,
    EHRAuthError,
    EHRConnectorError,
    EHRNotFoundError,
    EHRPatient,
    EHRServerError,
    EHRUnknownOutcomeError,
    EHRValidationError,
)

_TIMEOUT = httpx.Timeout(settings.mock_ehr_timeout_seconds)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _appointment_from_json(data: dict) -> EHRAppointment:
    return EHRAppointment(
        external_appointment_id=data["external_appointment_id"],
        external_patient_id=data["external_patient_id"],
        external_provider_id=data["external_provider_id"],
        source_platform_ref=data.get("source_platform_ref"),
        start_at=_parse_dt(data["start_at"]),
        end_at=_parse_dt(data["end_at"]),
        appointment_type=data["appointment_type"],
        status=data["status"],
        reason=data.get("reason"),
    )


class MockEHRConnector:
    connector_name = "mock_ehr"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: httpx.Timeout | None = None,
        client_factory=None,
    ):
        """client_factory: optional replacement for httpx.Client construction.
        Used by tests to route requests to an in-process ASGI app (no network).
        """
        self._base_url = (base_url or settings.mock_ehr_url).rstrip("/")
        self._api_key = api_key or settings.mock_ehr_api_key
        self._timeout = timeout or _TIMEOUT
        self._client_factory = client_factory or (lambda **kw: httpx.Client(**kw))

    # ---------------- internals ----------------

    def _headers(self, correlation_id: str | None, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"X-API-Key": self._api_key}
        if correlation_id:
            headers["X-Correlation-ID"] = correlation_id
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        correlation_id: str | None,
        idempotency_key: str | None = None,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        try:
            with self._client_factory(timeout=self._timeout, base_url=self._base_url) as client:
                response = client.request(
                    method,
                    path,
                    headers=self._headers(correlation_id, idempotency_key),
                    json=json_body,
                    params=params,
                )
        except httpx.TimeoutException as exc:
            # The request may or may not have been processed by the EHR.
            raise EHRUnknownOutcomeError(f"EHR request timed out: {path}") from exc
        except httpx.TransportError as exc:
            raise EHRUnknownOutcomeError(f"EHR unreachable: {exc.__class__.__name__}") from exc

        if response.status_code in (401, 403):
            raise EHRAuthError("EHR rejected the API key", status=401)
        if response.status_code == 404:
            raise EHRNotFoundError("Not found in EHR", status=404)
        if response.status_code == 422:
            detail = response.json().get("detail", "EHR rejected the request") if response.headers.get("content-type", "").startswith("application/json") else "EHR rejected the request"
            raise EHRValidationError(str(detail), status=422)
        if response.status_code >= 500:
            raise EHRServerError(f"EHR server error {response.status_code}", status=response.status_code)
        if response.status_code >= 400:
            raise EHRConnectorError(f"EHR error {response.status_code}", status=response.status_code)
        return response

    # ---------------- patients ----------------

    def create_patient(self, patient: EHRPatient, *, correlation_id: str | None = None) -> EHRPatient:
        response = self._request(
            "POST",
            "/ehr/patients",
            correlation_id=correlation_id,
            json_body={
                "external_patient_id": patient.external_patient_id,
                "full_name": patient.full_name,
                "date_of_birth": patient.date_of_birth,
                "phone": patient.phone,
                "email": patient.email,
            },
        )
        return self._patient_from(response)

    def _patient_from(self, response: httpx.Response) -> EHRPatient:
        data = response.json()
        return EHRPatient(
            external_patient_id=data["external_patient_id"],
            full_name=data["full_name"],
            date_of_birth=data.get("date_of_birth"),
            phone=data.get("phone"),
            email=data.get("email"),
        )

    def get_patient(self, external_patient_id: str, *, correlation_id: str | None = None) -> EHRPatient:
        return self._patient_from(
            self._request("GET", f"/ehr/patients/{external_patient_id}", correlation_id=correlation_id)
        )

    # ---------------- appointments ----------------

    def create_appointment(
        self,
        appointment: EHRAppointment,
        *,
        idempotency_key: str,
        correlation_id: str | None = None,
    ) -> tuple[EHRAppointment, bool]:
        """Create (or replay) an EHR appointment. Returns (appointment, replayed)."""
        response = self._request(
            "POST",
            "/ehr/appointments",
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            json_body={
                "external_patient_id": appointment.external_patient_id,
                "external_provider_id": appointment.external_provider_id,
                "start_at": appointment.start_at.isoformat(),
                "end_at": appointment.end_at.isoformat(),
                "appointment_type": appointment.appointment_type,
                "status": appointment.status,
                "reason": appointment.reason,
                "source_platform_ref": appointment.source_platform_ref,
            },
        )
        replayed = response.headers.get("X-EHR-Idempotent-Replay") == "true"
        return _appointment_from_json(response.json()), replayed

    def get_appointment(self, external_appointment_id: str, *, correlation_id: str | None = None) -> EHRAppointment:
        return _appointment_from_json(
            self._request("GET", f"/ehr/appointments/{external_appointment_id}", correlation_id=correlation_id).json()
        )

    def find_appointment_by_platform_ref(self, platform_ref: str, *, correlation_id: str | None = None) -> EHRAppointment | None:
        """Verification lookup. Returns None when the EHR has no such record
        (the decisive signal for Phase 4 reconciliation: not found → safe retry).
        """
        try:
            response = self._request(
                "GET", f"/ehr/appointments/by-platform-ref/{platform_ref}", correlation_id=correlation_id
            )
        except EHRNotFoundError:
            return None
        return _appointment_from_json(response.json())

    def update_appointment(
        self,
        external_appointment_id: str,
        *,
        status: str | None = None,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> EHRAppointment:
        body: dict = {}
        if status is not None:
            body["status"] = status
        if reason is not None:
            body["reason"] = reason
        return _appointment_from_json(
            self._request(
                "PATCH", f"/ehr/appointments/{external_appointment_id}", correlation_id=correlation_id, json_body=body
            ).json()
        )
