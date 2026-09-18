"""Phase 3 — Mock EHR + platform connector tests.

Run from repo root:  python -m unittest discover -s tests -v

Covers the 15 required scenarios:
  1 health  2 valid key  3 invalid key  4 patient creation  5 appointment creation
  6 appointment retrieval  7 appointment update  8 idempotent creation
  9 same key -> no duplicates  10 separate EHR database  11 connector works
  12 sync stores external id  13 server failure surfaced  14 timeout surfaced
  15 correlation/idempotency preserved

The connector talks to the Mock EHR app IN-PROCESS via httpx.ASGITransport
(no live server needed); fault injections use the real middleware plus
httpx.MockTransport for deterministic timeout simulation.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from datetime import date, datetime, time, timedelta
from unittest import mock

# Throwaway DBs BEFORE any app import. os.environ wins over .env in
# pydantic-settings, so these never touch the developer's data/ directory.
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp(prefix='hap-p3-')}/platform.db")
os.environ.setdefault("MOCK_EHR_DATABASE_URL", f"sqlite:///{tempfile.mkdtemp(prefix='hap-p3-')}/ehr.db")
os.environ.setdefault("MOCK_EHR_API_KEY", "test-ehr-key-3")
os.environ.setdefault("APP_ENVIRONMENT", "test")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.security import hash_password  # noqa: E402
from backend.app.database.base import engine as platform_engine  # noqa: E402
from backend.app.database.base import SessionLocal as PlatformSession  # noqa: E402
from backend.app.integrations.ehr.base import (  # noqa: E402
    EHRAppointment,
    EHRAuthError,
    EHRNotFoundError,
    EHRPatient,
    EHRServerError,
    EHRUnknownOutcomeError,
    EHRValidationError,
)
from backend.app.integrations.ehr.mock_ehr import MockEHRConnector  # noqa: E402
from backend.app.main import app as platform_app  # noqa: E402
from backend.app.models import (  # noqa: E402
    Appointment as PlatformAppointmentModel,
    Doctor,
    DoctorAvailability,
    Hospital,
    IntegrationOperation,
    Patient,
    User,
)
from backend.app.scheduling import SchedulingService  # noqa: E402
from backend.app.services import EHRSyncError, EHRSyncService  # noqa: E402
import mock_ehr.app.config as ehr_config  # noqa: E402
from mock_ehr.app.database import Base as EHRBase  # noqa: E402
from mock_ehr.app.database import SessionLocal as EHRSession  # noqa: E402
from mock_ehr.app.database import engine as ehr_engine  # noqa: E402
from mock_ehr.app.main import app as ehr_app  # noqa: E402
from mock_ehr.app.models import Appointment as EHRAppointmentRow  # noqa: E402
from mock_ehr.app.models import IdempotencyRecord, Provider  # noqa: E402
from mock_ehr.app.services.ehr_service import EHRService  # noqa: E402
from mock_ehr.app import schemas as ehr_schemas  # noqa: E402

API_KEY = os.environ["MOCK_EHR_API_KEY"]
platform_client = TestClient(platform_app)
ehr_client = TestClient(ehr_app)

# Local .env files may carry leftover fault-injection settings from manual
# failure demos; tests control faults explicitly (via _patch_fault_mode), so
# force the neutral state at import. This does not affect live services.
ehr_config.settings.mock_ehr_fault_mode = "none"
ehr_config.settings.mock_ehr_fault_paths = ""

EHR_TABLES = {"ehr_providers", "ehr_patients", "ehr_appointments", "idempotency_records"}
PLATFORM_TABLES = {"users", "hospitals", "doctors", "appointments", "integration_operations", "audit_events"}


def future_weekday(weekday: int, min_days_ahead: int = 35) -> date:
    day = date.today() + timedelta(days=min_days_ahead)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day


def at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm))


def make_connector(*, api_key: str | None = None, transport=None, timeout: float = 5.0) -> MockEHRConnector:
    """In-process connector: httpx talks straight to the Mock EHR ASGI app."""

    def factory(**kwargs):
        if transport is not None:
            kwargs["transport"] = transport
        return httpx.Client(**kwargs)

    return MockEHRConnector(
        base_url="http://ehr.test",
        api_key=api_key or API_KEY,
        timeout=httpx.Timeout(timeout),
        client_factory=factory,
    )


class _ASGISyncTransport(httpx.BaseTransport):
    """Sync httpx transport that executes requests against the in-process ASGI
    app (the stock ASGITransport is async-only; the connector is synchronous)."""

    def __init__(self, asgi_app) -> None:
        self._inner = httpx.ASGITransport(app=asgi_app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        import asyncio

        body = request.read()
        headers = httpx.Headers(request.headers)
        url = str(request.url)

        async def run() -> httpx.Response:
            async with httpx.AsyncClient(transport=self._inner) as client:
                return await client.request(request.method, url, headers=headers, content=body)

        response = asyncio.run(run())
        response.read()
        # Rebuild as a SYNC response: the AsyncClient's stream object is
        # async-only and httpx's sync client asserts on that.
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )


def asgi_transport() -> httpx.BaseTransport:
    return _ASGISyncTransport(ehr_app)


def seed_ehr_provider(external_provider_id: str, name: str = "Dr. Test Provider") -> None:
    db = EHRSession()
    try:
        existing = db.query(Provider).filter(Provider.external_provider_id == external_provider_id).first()
        if existing is None:
            db.add(Provider(external_provider_id=external_provider_id, full_name=name, specialty="orthopedics"))
            db.commit()
    finally:
        db.close()


class PlatformFixture:
    """Creates an approved hospital, a mapped doctor, and a patient; books a slot."""

    def __init__(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        db = PlatformSession()
        try:
            hospital = Hospital(name=f"EHR Hosp {suffix}", status="approved")
            db.add(hospital)
            db.flush()
            self.hospital_id = hospital.id

            admin = User(
                email=f"ehr-admin-{suffix}@test.health",
                password_hash=hash_password("TestPass1!"),
                full_name="EHR Admin",
                role="hospital_admin",
                hospital_id=hospital.id,
            )
            db.add(admin)
            db.flush()
            self.admin_email = admin.email

            doctor = Doctor(
                hospital_id=hospital.id,
                full_name=f"Dr. Sync {suffix}",
                specialty="orthopedics",
                consultation_minutes=30,
                appointment_types=["in_person", "video"],
                status="active",
                external_provider_id="EHR-PROV-TEST-1",
            )
            db.add(doctor)
            db.flush()
            self.doctor_id = doctor.id

            db.add(
                DoctorAvailability(
                    hospital_id=hospital.id,
                    doctor_id=doctor.id,
                    weekday=0,
                    start_time="09:00",
                    end_time="12:00",
                    slot_minutes=30,
                    appointment_types=["in_person", "video"],
                )
            )

            patient_user = User(
                email=f"ehr-patient-{suffix}@test.health",
                password_hash=hash_password("TestPass1!"),
                full_name="Sync Patient",
                role="patient",
            )
            db.add(patient_user)
            db.flush()
            patient = Patient(user_id=patient_user.id, phone="+91 90000 00000", date_of_birth=date(1991, 1, 2))
            db.add(patient)
            db.flush()
            self.patient_user_email = patient_user.email
            self.patient_id = patient.id

            self.monday = future_weekday(0)
            db.commit()
        finally:
            db.close()

    def book(self, hh: int = 10, mm: int = 0, with_provider: bool = True) -> PlatformAppointmentModel:
        db = PlatformSession()
        try:
            if not with_provider:
                db.get(Doctor, self.doctor_id).external_provider_id = None
                db.commit()
            appt = SchedulingService(db).book_appointment(
                patient_id=self.patient_id,
                doctor_id=self.doctor_id,
                start_at=at(self.monday, hh, mm),
            )
            db.expunge_all()
            return appt
        finally:
            db.close()


class TestMockEHRServiceAPI(unittest.TestCase):
    """The Mock EHR as an independent HTTP service."""

    monday = future_weekday(0)

    @classmethod
    def setUpClass(cls) -> None:
        EHRBase.metadata.create_all(ehr_engine)
        seed_ehr_provider("EHR-PROV-TEST-1")

    def setUp(self) -> None:
        self.suffix = uuid.uuid4().hex[:8]

    def _patient_payload(self) -> dict:
        return {
            "external_patient_id": f"PLAT-{self.suffix}",
            "full_name": f"Patient {self.suffix}",
            "date_of_birth": "1990-05-14",
            "phone": "+91 90000 11111",
        }

    def _appt_payload(self, **overrides) -> dict:
        start = at(self.monday, 15, 0).isoformat()
        payload = {
            "external_patient_id": f"PLAT-{self.suffix}",
            "external_provider_id": "EHR-PROV-TEST-1",
            "start_at": start,
            "end_at": at(self.monday, 15, 30).isoformat(),
            "appointment_type": "in_person",
            "status": "booked",
            "reason": "shoulder pain",
            "source_platform_ref": f"plat-appt-{self.suffix}",
        }
        payload.update(overrides)
        return payload

    def _post_appt(self, payload: dict | None = None, key: str | None = None) -> httpx.Response:
        # Unique key per test: idempotency keys are globally unique in the EHR DB.
        return ehr_client.post(
            "/ehr/appointments",
            json=payload or self._appt_payload(),
            headers={"X-API-Key": API_KEY, "Idempotency-Key": key or f"idem-{self.suffix}"},
        )

    # --- 1-3: health + API key ---------------------------------------

    def test_1_health_public(self) -> None:
        resp = ehr_client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["service"], "mock_ehr")

    def test_2_valid_api_key_accepted(self) -> None:
        resp = ehr_client.get("/ehr/providers", headers={"X-API-Key": API_KEY})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(p["external_provider_id"] == "EHR-PROV-TEST-1" for p in resp.json()))

    def test_3_invalid_or_missing_api_key_rejected(self) -> None:
        self.assertEqual(ehr_client.get("/ehr/providers", headers={"X-API-Key": "wrong"}).status_code, 401)
        self.assertEqual(ehr_client.get("/ehr/providers").status_code, 401)

    # --- 4-7: CRUD ----------------------------------------------------

    def test_4_patient_creation(self) -> None:
        resp = ehr_client.post("/ehr/patients", json=self._patient_payload(), headers={"X-API-Key": API_KEY})
        self.assertEqual(resp.status_code, 201)
        got = ehr_client.get(
            f"/ehr/patients/{self._patient_payload()['external_patient_id']}", headers={"X-API-Key": API_KEY}
        )
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()["full_name"], self._patient_payload()["full_name"])

    def test_5_appointment_creation(self) -> None:
        ehr_client.post("/ehr/patients", json=self._patient_payload(), headers={"X-API-Key": API_KEY})
        resp = self._post_appt()
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertTrue(resp.json()["external_appointment_id"].startswith("EHR-"))
        self.assertEqual(resp.headers.get("X-EHR-Idempotent-Replay"), "false")

    def test_6_appointment_retrieval(self) -> None:
        ehr_client.post("/ehr/patients", json=self._patient_payload(), headers={"X-API-Key": API_KEY})
        appt_id = self._post_appt().json()["external_appointment_id"]
        got = ehr_client.get(f"/ehr/appointments/{appt_id}", headers={"X-API-Key": API_KEY})
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()["external_appointment_id"], appt_id)
        missing = ehr_client.get("/ehr/appointments/EHR-does-not-exist", headers={"X-API-Key": API_KEY})
        self.assertEqual(missing.status_code, 404)

    def test_7_appointment_update(self) -> None:
        ehr_client.post("/ehr/patients", json=self._patient_payload(), headers={"X-API-Key": API_KEY})
        appt_id = self._post_appt().json()["external_appointment_id"]
        patched = ehr_client.patch(
            f"/ehr/appointments/{appt_id}",
            json={"status": "cancelled", "reason": "patient cancelled"},
            headers={"X-API-Key": API_KEY},
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(patched.json()["status"], "cancelled")
        with_invalid = ehr_client.patch(
            f"/ehr/appointments/{appt_id}", json={"status": "bogus"}, headers={"X-API-Key": API_KEY}
        )
        self.assertEqual(with_invalid.status_code, 422)

    # --- 8-9: idempotency ---------------------------------------------

    def test_8_idempotent_replay_same_result(self) -> None:
        ehr_client.post("/ehr/patients", json=self._patient_payload(), headers={"X-API-Key": API_KEY})
        payload = self._appt_payload()
        first = self._post_appt(payload)
        replay = self._post_appt(payload)  # same key, same body
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.headers.get("X-EHR-Idempotent-Replay"), "true")
        self.assertEqual(
            first.json()["external_appointment_id"], replay.json()["external_appointment_id"]
        )

    def test_9_same_key_never_duplicates(self) -> None:
        ehr_client.post("/ehr/patients", json=self._patient_payload(), headers={"X-API-Key": API_KEY})
        payload = self._appt_payload()
        for _ in range(3):
            self._post_appt(payload)
        db = EHRSession()
        try:
            self.assertEqual(db.query(EHRAppointmentRow).filter(
                EHRAppointmentRow.source_platform_ref == payload["source_platform_ref"]).count(), 1)
            self.assertEqual(db.query(IdempotencyRecord).filter(
                IdempotencyRecord.idempotency_key == f"idem-{self.suffix}").count(), 1)
        finally:
            db.close()

    def test_9b_replay_with_different_payload_conflicts(self) -> None:
        ehr_client.post("/ehr/patients", json=self._patient_payload(), headers={"X-API-Key": API_KEY})
        self._post_appt(self._appt_payload())
        conflict = self._post_appt(self._appt_payload(reason="different reason"))
        self.assertEqual(conflict.status_code, 409)

    def test_9c_platform_ref_dedupe_across_keys(self) -> None:
        ehr_client.post("/ehr/patients", json=self._patient_payload(), headers={"X-API-Key": API_KEY})
        payload = self._appt_payload()
        first = self._post_appt(payload, key=f"{self.suffix}-a")
        dedupe = self._post_appt(payload, key=f"{self.suffix}-b")  # new key, same platform ref
        self.assertEqual(dedupe.status_code, 200)
        self.assertEqual(
            first.json()["external_appointment_id"], dedupe.json()["external_appointment_id"]
        )

    # --- fault injection ----------------------------------------------

    def _with_fault(self, mode: str):
        return _patch_fault_mode(mode)

    def test_13_fault_server_error_not_processed(self) -> None:
        with _patch_fault_mode("server_error"):
            resp = ehr_client.post(
                "/ehr/patients", json=self._patient_payload(), headers={"X-API-Key": API_KEY}
            )
        self.assertEqual(resp.status_code, 500)
        got = ehr_client.get(
            f"/ehr/patients/{self._patient_payload()['external_patient_id']}", headers={"X-API-Key": API_KEY}
        )
        self.assertEqual(got.status_code, 404)  # nothing was written

    def test_fault_rejected_not_processed(self) -> None:
        with _patch_fault_mode("rejected"):
            resp = self._post_appt(self._appt_payload())
        self.assertEqual(resp.status_code, 422)
        db = EHRSession()
        try:
            self.assertEqual(db.query(EHRAppointmentRow).filter(
                EHRAppointmentRow.source_platform_ref == self._appt_payload()["source_platform_ref"]).count(), 0)
        finally:
            db.close()

    def test_fault_timeout_sleeps_then_processes(self) -> None:
        """Timeout mode lets the write LAND after the delay — the exact situation
        Phase 4's unknown-outcome recovery must resolve."""
        with _patch_fault_mode("timeout", delay_seconds=0.05):
            ehr_client.post("/ehr/patients", json=self._patient_payload(), headers={"X-API-Key": API_KEY})
            resp = self._post_appt(self._appt_payload())
        self.assertEqual(resp.status_code, 201, resp.text)

    # --- 10: database separation ---------------------------------------

    def test_10_separate_databases(self) -> None:
        self.assertNotEqual(platform_engine.url, ehr_engine.url)
        ehr_tables = set(EHRBase.metadata.tables.keys())
        self.assertTrue(EHR_TABLES.issubset(ehr_tables))
        platform_tables = set(PlatformAppointmentModel.metadata.tables.keys())
        self.assertTrue(PLATFORM_TABLES.issubset(platform_tables))
        self.assertFalse(EHR_TABLES & platform_tables)  # no EHR tables in platform metadata
        # An appointment created at the EHR exists ONLY in the EHR database.
        ehr_client.post("/ehr/patients", json=self._patient_payload(), headers={"X-API-Key": API_KEY})
        self._post_appt(self._appt_payload())
        db = EHRSession()
        try:
            self.assertEqual(db.query(EHRAppointmentRow).filter(
                EHRAppointmentRow.source_platform_ref == self._appt_payload()["source_platform_ref"]).count(), 1)
        finally:
            db.close()


class _patch_fault_mode:
    """Deterministically toggles the EHR's fault middleware (restores after)."""

    def __init__(self, mode: str, delay_seconds: float | None = None) -> None:
        self.mode = mode
        self.delay = delay_seconds
        self._originals: list = []

    def __enter__(self):
        self._originals.append((ehr_config.settings, "mock_ehr_fault_mode", ehr_config.settings.mock_ehr_fault_mode))
        ehr_config.settings.mock_ehr_fault_mode = self.mode
        if self.delay is not None:
            self._originals.append(
                (ehr_config.settings, "mock_ehr_fault_delay_seconds", ehr_config.settings.mock_ehr_fault_delay_seconds)
            )
            ehr_config.settings.mock_ehr_fault_delay_seconds = self.delay
        return self

    def __exit__(self, *exc) -> None:
        for obj, attr, value in reversed(self._originals):
            setattr(obj, attr, value)


class TestPlatformConnector(unittest.TestCase):
    """11 — connector round trip + 13/14 — typed error mapping."""

    def setUp(self) -> None:
        EHRBase.metadata.create_all(ehr_engine)
        seed_ehr_provider("EHR-PROV-TEST-1")

    def test_11_patient_round_trip(self) -> None:
        connector = make_connector(transport=asgi_transport())
        patient = EHRPatient(external_patient_id="PLAT-rt-1", full_name="Round Trip", phone="+91 1")
        created = connector.create_patient(patient, correlation_id="corr-rt")
        self.assertEqual(created.external_patient_id, "PLAT-rt-1")
        fetched = connector.get_patient("PLAT-rt-1", correlation_id="corr-rt")
        self.assertEqual(fetched.full_name, "Round Trip")

    def test_11_appointment_round_trip_replay_flag(self) -> None:
        connector = make_connector(transport=asgi_transport())
        connector.create_patient(EHRPatient(external_patient_id="PLAT-rt-2", full_name="RT2"), correlation_id="c")
        appt = EHRAppointment(
            external_appointment_id="",
            external_patient_id="PLAT-rt-2",
            external_provider_id="EHR-PROV-TEST-1",
            source_platform_ref="plat-rt-2",
            start_at=at(self.monday if hasattr(self, "monday") else future_weekday(0), 16, 0),
            end_at=at(future_weekday(0), 16, 30),
            appointment_type="in_person",
            status="booked",
        )
        created, replayed = connector.create_appointment(appt, idempotency_key="rt-key", correlation_id="c")
        self.assertFalse(replayed)
        self.assertTrue(created.external_appointment_id.startswith("EHR-"))
        again, replayed2 = connector.create_appointment(appt, idempotency_key="rt-key", correlation_id="c")
        self.assertTrue(replayed2)
        self.assertEqual(again.external_appointment_id, created.external_appointment_id)

    def test_connector_get_unknown_appointment_raises_not_found(self) -> None:
        connector = make_connector(transport=asgi_transport())
        with self.assertRaises(EHRNotFoundError):
            connector.get_appointment("EHR-missing", correlation_id="c")

    def test_14_timeout_raises_unknown_outcome(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("simulated timeout", request=request)

        connector = make_connector(
            transport=httpx.MockTransport(handler), timeout=0.05
        )
        with self.assertRaises(EHRUnknownOutcomeError):
            connector.get_appointment("EHR-x", correlation_id="c")

    def test_13_server_error_raises_ehr_server_error(self) -> None:
        connector = make_connector(transport=httpx.MockTransport(lambda request: httpx.Response(500, json={"detail": "boom"})))
        with self.assertRaises(EHRServerError):
            connector.get_appointment("EHR-x", correlation_id="c")

    def test_connector_validation_error_mapping(self) -> None:
        connector = make_connector(transport=httpx.MockTransport(lambda request: httpx.Response(422, json={"detail": "bad"})))
        with self.assertRaises(EHRValidationError):
            connector.get_appointment("EHR-x", correlation_id="c")

    def test_connector_auth_error_mapping(self) -> None:
        connector = make_connector(transport=asgi_transport(), api_key="definitely-wrong")
        with self.assertRaises(EHRAuthError):
            connector.get_patient("PLAT-any", correlation_id="c")


class TestEHRSyncService(unittest.TestCase):
    """12-15: sync outcomes synced/failed/unknown + observability records."""

    @classmethod
    def setUpClass(cls) -> None:
        EHRBase.metadata.create_all(ehr_engine)
        seed_ehr_provider("EHR-PROV-TEST-1")

    def setUp(self) -> None:
        self.fixture = PlatformFixture()

    def _service(self, connector: MockEHRConnector | None = None) -> EHRSyncService:
        db = PlatformSession()
        self.addCleanup(db.close)
        return EHRSyncService(db, connector=connector or make_connector(transport=asgi_transport()))

    def test_12_sync_stores_external_id_and_logs_operation(self) -> None:
        appt = self.fixture.book()
        synced = self._service().sync_appointment(appt.id)
        self.assertEqual(synced.ehr_sync_status, "synced")
        self.assertTrue((synced.external_ehr_appointment_id or "").startswith("EHR-"))
        self.assertIsNotNone(synced.ehr_synced_at)
        self.assertTrue((synced.ehr_idempotency_key or "").startswith("plat-appt-"))

        op = (
            PlatformSession()
            .query(IntegrationOperation)
            .filter(IntegrationOperation.resource_id == appt.id)
            .order_by(IntegrationOperation.created_at.desc())
            .first()
        )
        self.assertIsNotNone(op)
        self.assertEqual(op.outcome, "success")
        self.assertEqual(op.operation, "ehr.create_appointment")
        self.assertEqual(op.idempotency_key, synced.ehr_idempotency_key)
        self.assertIsNotNone(op.correlation_id)
        self.assertIsNotNone(op.duration_ms)

        # The EHR row exists, keyed by platform ref, with correlation preserved.
        db = EHRSession()
        try:
            row = (
                db.query(EHRAppointmentRow)
                .filter(EHRAppointmentRow.source_platform_ref == appt.id)
                .one()
            )
            self.assertEqual(row.external_appointment_id, synced.external_ehr_appointment_id)
            self.assertEqual(row.correlation_id, op.correlation_id)
        finally:
            db.close()

    def test_sync_is_guarded_against_double_sync(self) -> None:
        appt = self.fixture.book()
        service = self._service()
        service.sync_appointment(appt.id)
        with self.assertRaises(EHRSyncError) as ctx:
            service.sync_appointment(appt.id)
        self.assertEqual(ctx.exception.reason, "already_synced")

    def test_13_server_failure_marks_failed_and_writes_nothing(self) -> None:
        appt = self.fixture.book()
        connector = make_connector(
            transport=httpx.MockTransport(lambda request: httpx.Response(500, json={"detail": "boom"}))
        )
        synced = self._service(connector).sync_appointment(appt.id)
        self.assertEqual(synced.ehr_sync_status, "failed")
        self.assertIsNone(synced.external_ehr_appointment_id)
        op = (
            PlatformSession()
            .query(IntegrationOperation)
            .filter(IntegrationOperation.resource_id == appt.id)
            .order_by(IntegrationOperation.created_at.desc())
            .first()
        )
        self.assertEqual(op.outcome, "failed")
        self.assertEqual(op.error_code, "ehr_server_error")
        db = EHRSession()
        try:
            self.assertEqual(
                db.query(EHRAppointmentRow).filter(EHRAppointmentRow.source_platform_ref == appt.id).count(), 0
            )
        finally:
            db.close()

    def test_14_timeout_marks_unknown_but_write_lands_and_is_findable(self) -> None:
        """The Phase 4 seed scenario: connector times out, yet the EHR processed
        the write. Sync must record 'unknown' (never 'failed'), and verification
        by platform ref must find the truth."""
        ehr_db = EHRSession()
        inner = asgi_transport()

        def handler(request: httpx.Request) -> httpx.Response:
            # Only the appointment POST simulates "EHR processed the write, then
            # the response was lost". Everything else routes to the real app.
            if request.method == "POST" and request.url.path == "/ehr/appointments":
                payload = json.loads(request.read().decode())
                EHRService(ehr_db).create_appointment(
                    ehr_schemas.AppointmentCreate(**payload),
                    idempotency_key=request.headers.get("Idempotency-Key"),
                    correlation_id=request.headers.get("X-Correlation-ID"),
                )
                raise httpx.ConnectTimeout("simulated timeout after write", request=request)
            return inner.handle_request(request)

        appt = self.fixture.book()
        connector = make_connector(transport=httpx.MockTransport(handler), timeout=0.05)
        synced = self._service(connector).sync_appointment(appt.id)
        self.assertEqual(synced.ehr_sync_status, "unknown")
        self.assertIsNone(synced.external_ehr_appointment_id)
        op = (
            PlatformSession()
            .query(IntegrationOperation)
            .filter(IntegrationOperation.resource_id == appt.id)
            .order_by(IntegrationOperation.created_at.desc())
            .first()
        )
        self.assertEqual(op.outcome, "unknown")
        self.assertEqual(op.error_code, "ehr_unknown_outcome")
        self.assertEqual(op.idempotency_key, synced.ehr_idempotency_key)

        # Verification hook for Phase 4: query the EHR by platform ref.
        finder = make_connector(transport=asgi_transport())
        found = finder.find_appointment_by_platform_ref(appt.id, correlation_id=op.correlation_id)
        self.assertIsNotNone(found)
        self.assertTrue(found.external_appointment_id.startswith("EHR-"))

    def test_sync_requires_provider_mapping(self) -> None:
        appt = self.fixture.book(with_provider=False)
        with self.assertRaises(EHRSyncError) as ctx:
            self._service().sync_appointment(appt.id)
        self.assertEqual(ctx.exception.reason, "doctor_missing_external_provider_id")

    def test_sync_cancelled_appointment_rejected(self) -> None:
        appt = self.fixture.book()
        db = PlatformSession()
        try:
            SchedulingService(db).cancel_appointment(appt.id)
        finally:
            db.close()
        with self.assertRaises(EHRSyncError) as ctx:
            self._service().sync_appointment(appt.id)
        self.assertEqual(ctx.exception.reason, "appointment_cancelled")


class TestIntegrationRoutes(unittest.TestCase):
    """HTTP authorization + tenant isolation on the integration endpoints."""

    @classmethod
    def setUpClass(cls) -> None:
        EHRBase.metadata.create_all(ehr_engine)
        seed_ehr_provider("EHR-PROV-TEST-1")

    def setUp(self) -> None:
        self.fixture = PlatformFixture()

        # A second hospital + admin, for isolation checks.
        suffix = uuid.uuid4().hex[:8]
        db = PlatformSession()
        try:
            other = Hospital(name=f"Other Hosp {suffix}", status="approved")
            db.add(other)
            db.flush()
            db.add(
                User(
                    email=f"other-admin-{suffix}@test.health",
                    password_hash=hash_password("TestPass1!"),
                    full_name="Other Admin",
                    role="hospital_admin",
                    hospital_id=other.id,
                )
            )
            db.commit()
            self.other_admin_email = f"other-admin-{suffix}@test.health"
        finally:
            db.close()

    def _login(self, email: str) -> dict:
        resp = platform_client.post("/auth/login", json={"email": email, "password": "TestPass1!"})
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def test_sync_requires_auth(self) -> None:
        appt = self.fixture.book()
        self.assertEqual(platform_client.post(f"/appointments/{appt.id}/sync-ehr").status_code, 401)
        self.assertEqual(platform_client.get(f"/appointments/{appt.id}/ehr-status").status_code, 401)
        self.assertEqual(platform_client.get("/integrations/operations").status_code, 401)

    def test_patient_can_sync_and_verify_own_appointment(self) -> None:
        appt = self.fixture.book()
        headers = self._login(self.fixture.patient_user_email)
        connector = make_connector(transport=asgi_transport())
        with mock.patch("backend.app.services.build_ehr_connector", return_value=connector):
            resp = platform_client.post(f"/appointments/{appt.id}/sync-ehr", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["ehr_sync_status"], "synced")
        external_id = resp.json()["external_ehr_appointment_id"]
        self.assertTrue(external_id.startswith("EHR-"))

        with mock.patch("backend.app.services.build_ehr_connector", return_value=connector):
            status = platform_client.get(f"/appointments/{appt.id}/ehr-status", headers=headers)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["ehr_status"], "booked")
        self.assertEqual(status.json()["external_ehr_appointment_id"], external_id)

        # Second sync -> 409 already_synced, still exactly one EHR row.
        with mock.patch("backend.app.services.build_ehr_connector", return_value=connector):
            again = platform_client.post(f"/appointments/{appt.id}/sync-ehr", headers=headers)
        self.assertEqual(again.status_code, 409)
        db = EHRSession()
        try:
            self.assertEqual(
                db.query(EHRAppointmentRow).filter(EHRAppointmentRow.source_platform_ref == appt.id).count(), 1
            )
        finally:
            db.close()

    def test_other_hospital_admin_cannot_access(self) -> None:
        appt = self.fixture.book()
        headers = self._login(self.other_admin_email)
        self.assertEqual(platform_client.post(f"/appointments/{appt.id}/sync-ehr", headers=headers).status_code, 403)
        self.assertEqual(platform_client.get(f"/appointments/{appt.id}/ehr-status", headers=headers).status_code, 403)

    def test_operations_log_is_tenant_scoped(self) -> None:
        appt = self.fixture.book()
        connector = make_connector(transport=asgi_transport())
        with mock.patch("backend.app.services.build_ehr_connector", return_value=connector):
            platform_client.post(
                f"/appointments/{appt.id}/sync-ehr", headers=self._login(self.fixture.patient_user_email)
            )
        own = platform_client.get(
            "/integrations/operations", headers=self._login(self.fixture.admin_email)
        )
        self.assertEqual(own.status_code, 200)
        self.assertTrue(any(op["resource_id"] == appt.id for op in own.json()))

        other = platform_client.get(
            "/integrations/operations", headers=self._login(self.other_admin_email)
        )
        self.assertEqual(other.status_code, 200)
        self.assertFalse(any(op["resource_id"] == appt.id for op in other.json()))

        # Patients cannot browse the integration log.
        self.assertEqual(
            platform_client.get(
                "/integrations/operations", headers=self._login(self.fixture.patient_user_email)
            ).status_code,
            403,
        )


if __name__ == "__main__":
    unittest.main()
