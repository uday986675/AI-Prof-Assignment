"""Pydantic schemas for EHR integration endpoints."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class EHRSyncOut(BaseModel):
    id: str
    status: str
    ehr_sync_status: str | None
    external_ehr_appointment_id: str | None
    ehr_idempotency_key: str | None
    ehr_synced_at: datetime | None

    model_config = {"from_attributes": True}


class EHRStatusOut(BaseModel):
    appointment_id: str
    ehr_sync_status: str | None
    external_ehr_appointment_id: str | None
    ehr_status: str | None
    start_at: str | None
    provider_id: str | None


class IntegrationOperationOut(BaseModel):
    id: str
    operation: str
    connector: str
    outcome: str
    error_code: str | None
    error_detail: str | None
    resource_type: str | None
    resource_id: str | None
    idempotency_key: str | None
    correlation_id: str | None
    duration_ms: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class EHRReconcileOut(BaseModel):
    id: str
    status: str
    ehr_sync_status: str | None
    external_ehr_appointment_id: str | None
    ehr_idempotency_key: str | None
    ehr_synced_at: datetime | None

    model_config = {"from_attributes": True}
