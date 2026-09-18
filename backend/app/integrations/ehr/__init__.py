"""Platform ↔ EHR integration — the ONE controlled boundary to external EHRs.

Services/routes depend on the EHRConnector protocol; only the factory knows
which implementation is active (EHR_MODE=mock → MockEHRConnector over HTTP).
The AI agent will later receive capabilities that use this package — it never
touches HTTP or the EHR directly.
"""
from __future__ import annotations

from .base import (
    EHRAppointment,
    EHRAuthError,
    EHRConnector,
    EHRConnectorError,
    EHRNotFoundError,
    EHRPatient,
    EHRServerError,
    EHRUnavailableError,
    EHRUnknownOutcomeError,
    EHRValidationError,
    build_ehr_connector,
)

__all__ = [
    "EHRConnector",
    "EHRAppointment",
    "EHRPatient",
    "EHRConnectorError",
    "EHRAuthError",
    "EHRValidationError",
    "EHRNotFoundError",
    "EHRServerError",
    "EHRUnavailableError",
    "EHRUnknownOutcomeError",
    "build_ehr_connector",
]
