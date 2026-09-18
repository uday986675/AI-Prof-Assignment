"""Phase 4 — booking reliability & unknown-outcome recovery tests.

Run from repo root:  python -m unittest discover -s tests -v

Groups (mirroring the Phase 4 spec):
  A  revalidation before booking            (tests 1-6)
  B  normal EHR synchronization + verify    (tests 7-10)
  C  definitive failures + safe retry       (tests 11-14)
  D  unknown outcome → reconciliation ADOPT (tests 15-20)
  E  unknown + NOT FOUND → safe retry       (tests 21-26)
  F  repeated recovery is idempotent        (tests 27-29)
  G  security / tenant isolation            (tests 30-32)

The connector talks to the Mock EHR app IN-PROCESS (same _ASGISyncTransport
bridge as Phase 3). Timeout-with-landed-write is simulated deterministically by
performing the EHR write in the transport handler, then raising ConnectTimeout.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from datetime import date, datetime, time, timedelta
from unittest import mock

# Throwaway DBs BEFORE any app import (same pattern as Phase 3).
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp(prefix='hap-p4-')}/platform.db")
os.environ.setdefault("MOCK_EHR_DATABASE_URL", f"sqlite:///{tempfile.mkdtemp(prefix='hap-p4-')}/ehr.db")
os.environ.setdefault("MOCK_EHR_API_KEY", "test-ehr-key-4")
os.environ.setdefault("APP_ENVIRONMENT", "test")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.security import hash_password  # noqa: E402
from backend.app.database.base import SessionLocal as PlatformSession  # noqa: E402
from backend.app.integrations.ehr.mock_ehr import MockEHRConnector  # noqa: E402
from backend.app.main import app as platform_app  # noqa: E402
from backend.app.models import (  # noqa: E402
    Appointment as PlatformAppointmentModel,
    BlockedPeriod,
    Doctor,
    EHR_SYNC_TRANSITIONS,
    Hospital,
    IntegrationOperation,
    Patient,
    User,
)
from backend.app.scheduling import SchedulingError, SchedulingService  # noqa: E402
from backend.app.services import (  # noqa: E402
    EHRRecoveryService,
    EHRSyncError,
    EHRSyncService,
    SYNC_STATUSES,
)
from tests.test_phase3_ehr import (  # noqa: E402  (reuse the proven Phase 3 harness)
    PlatformFixture,
    at,
    make_connector,
    seed_ehr_provider,
    asgi_transport,
)
from mock_ehr.app.database import Base as EHRBase  # noqa: E402
from mock_ehr.app.database import SessionLocal as EHRSession  # noqa: E402
from mock_ehr.app.database import engine as ehr_engine  # noqa: E402
from mock_ehr.app.models import Appointment as EHRAppointmentRow  # noqa: E402
from mock_ehr.app.services.ehr_service import EHRService  # noqa: E402
from mock_ehr.app import schemas as ehr_schemas  # noqa: E402

platform_client = TestClient(platform_app)

PROVIDER = "EHR-PROV-TEST-1"


def ehr_appt_count(platform_ref: str) -> int:
    db = EHRSession()
    try:
        return db.query(EHRAppointmentRow).filter(EHRAppointmentRow.source_platform_ref == platform_ref).count()
    finally:
        db.close()


def last_op(appointment_id: str) -> IntegrationOperation:
    return (
        PlatformSession()
        .query(IntegrationOperation)
        .filter(IntegrationOperation.resource_id == appointment_id)
        .order_by(IntegrationOperation.created_at.desc())
        .first()
    )


def timeout_after_write_transport() -> httpx.MockTransport:
    """'EHR processed the write, then the response was lost' — the exact
    unknown-outcome situation. Only the appointment POST is hijacked; the
    patient upsert passes through to the real in-process app."""
    ehr_db = EHRSession()
    inner = asgi_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/ehr/appointments":
            payload = json.loads(request.read().decode())
            EHRService(ehr_db).create_appointment(
                ehr_schemas.AppointmentCreate(**payload),
                idempotency_key=request.headers.get("Idempotency-Key"),
                correlation_id=request.headers.get("X-Correlation-ID"),
            )
            raise httpx.ConnectTimeout("simulated timeout after write", request=request)
        return inner.handle_request(request)

    return httpx.MockTransport(handler)


def lost_everywhere_transport() -> httpx.MockTransport:
    """Total network loss: nothing (patient or appointment) reaches the EHR."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated total outage", request=request)

    return httpx.MockTransport(handler)


class TestARevalidation(unittest.TestCase):
    """A — the booking path revalidates (1-6). Phase 2 covered these rules in
    the service; here we re-prove them through SchedulingService.book_appointment
    as Phase 4's workflow entry point, including the unapproved-hospital gate."""

    def setUp(self) -> None:
        self.fixture = PlatformFixture()

    def _book(self):
        return SchedulingService(PlatformSession()).book_appointment(
            patient_id=self.fixture.patient_id,
            doctor_id=self.fixture.doctor_id,
            start_at=at(self.fixture.monday, 10, 0),
        )

    def test_1_valid_slot_books_successfully(self) -> None:
        db = PlatformSession()
        try:
            appt = SchedulingService(db).book_appointment(
                patient_id=self.fixture.patient_id,
                doctor_id=self.fixture.doctor_id,
                start_at=at(self.fixture.monday, 9, 0),
            )
            self.assertEqual(appt.status, "booked")
        finally:
            db.close()

    def test_2_already_booked_slot_rejected_409(self) -> None:
        self._book()
        with self.assertRaises(SchedulingError) as ctx:
            self._book()
        self.assertEqual(ctx.exception.reason, "slot_already_booked")

    def test_3_outside_working_hours_rejected(self) -> None:
        with self.assertRaises(SchedulingError) as ctx:
            self._book_at(13, 0)
        self.assertEqual(ctx.exception.reason, "outside_working_hours")

    def _book_at(self, hh: int, mm: int = 0):
        return SchedulingService(PlatformSession()).book_appointment(
            patient_id=self.fixture.patient_id,
            doctor_id=self.fixture.doctor_id,
            start_at=at(self.fixture.monday, hh, mm),
        )

    def test_4_blocked_slot_rejected(self) -> None:
        db = PlatformSession()
        try:
            db.add(
                BlockedPeriod(
                    hospital_id=self.fixture.hospital_id,
                    doctor_id=self.fixture.doctor_id,
                    kind="blocked",
                    start_at=at(self.fixture.monday, 10, 0),
                    end_at=at(self.fixture.monday, 11, 0),
                )
            )
            db.commit()
        finally:
            db.close()
        with self.assertRaises(SchedulingError) as ctx:
            self._book_at(10, 0)
        self.assertEqual(ctx.exception.reason, "slot_blocked")

    def test_5_inactive_doctor_rejected(self) -> None:
        db = PlatformSession()
        try:
            db.get(Doctor, self.fixture.doctor_id).status = "inactive"
            db.commit()
        finally:
            db.close()
        with self.assertRaises(SchedulingError) as ctx:
            self._book()
        self.assertEqual(ctx.exception.reason, "doctor_inactive")

    def test_6_unapproved_hospital_rejected(self) -> None:
        db = PlatformSession()
        try:
            db.get(Hospital, self.fixture.hospital_id).status = "pending"
            db.commit()
        finally:
            db.close()
        with self.assertRaises(SchedulingError):
            self._book()


class TestBNormalSync(unittest.TestCase):
    """B — successful sync with verification (7-10)."""

    @classmethod
    def setUpClass(cls) -> None:
        EHRBase.metadata.create_all(ehr_engine)
        seed_ehr_provider(PROVIDER)

    def setUp(self) -> None:
        self.fixture = PlatformFixture()
        self.appt = self.fixture.book()

    def _service(self, connector=None) -> EHRSyncService:
        db = PlatformSession()
        self.addCleanup(db.close)
        return EHRSyncService(db, connector=connector or make_connector(transport=asgi_transport()))

    def test_7_successful_sync(self) -> None:
        synced = self._service().sync_appointment(self.appt.id)
        self.assertEqual(synced.ehr_sync_status, "synced")

    def test_8_external_id_stored(self) -> None:
        synced = self._service().sync_appointment(self.appt.id)
        self.assertTrue((synced.external_ehr_appointment_id or "").startswith("EHR-"))
        self.assertIsNotNone(synced.ehr_synced_at)

    def test_9_verification_recorded(self) -> None:
        self._service().sync_appointment(self.appt.id)
        op = last_op(self.appt.id)
        self.assertEqual(op.outcome, "success")
        self.assertTrue(op.response_summary["verified"])

    def test_10_resync_synced_appointment_returns_existing_state(self) -> None:
        service = self._service()
        first = service.sync_appointment(self.appt.id)
        with self.assertRaises(EHRSyncError) as ctx:
            service.sync_appointment(self.appt.id)
        self.assertEqual(ctx.exception.reason, "already_synced")
        # ...and reconciliation of an already-synced appointment is a no-op
        # that does NOT call the EHR create path again.
        db = PlatformSession()
        try:
            recovered = EHRRecoveryService(db, connector=make_connector(transport=asgi_transport())).reconcile_appointment(self.appt.id)
            self.assertEqual(recovered.external_ehr_appointment_id, first.external_ehr_appointment_id)
        finally:
            db.close()
        self.assertEqual(ehr_appt_count(self.appt.id), 1)


class TestCDefinitiveFailure(unittest.TestCase):
    """C — server error → failed → safe retry with the SAME key (11-14)."""

    @classmethod
    def setUpClass(cls) -> None:
        EHRBase.metadata.create_all(ehr_engine)
        seed_ehr_provider(PROVIDER)

    def setUp(self) -> None:
        self.fixture = PlatformFixture()
        self.appt = self.fixture.book()

    def _service(self, connector) -> EHRSyncService:
        db = PlatformSession()
        self.addCleanup(db.close)
        return EHRSyncService(db, connector=connector)

    def test_11_server_error_marks_failed(self) -> None:
        boom = make_connector(
            transport=httpx.MockTransport(lambda request: httpx.Response(500, json={"detail": "boom"}))
        )
        synced = self._service(boom).sync_appointment(self.appt.id)
        self.assertEqual(synced.ehr_sync_status, "failed")
        self.assertIsNone(synced.external_ehr_appointment_id)
        op = last_op(self.appt.id)
        self.assertEqual(op.outcome, "failed")
        self.assertEqual(op.error_code, "ehr_server_error")
        self.assertEqual(ehr_appt_count(self.appt.id), 0)

    def test_12_13_failed_retry_succeeds_with_stable_key(self) -> None:
        boom = make_connector(
            transport=httpx.MockTransport(lambda request: httpx.Response(500, json={"detail": "boom"}))
        )
        failed = self._service(boom).sync_appointment(self.appt.id)
        key_after_failure = failed.ehr_idempotency_key

        db = PlatformSession()
        try:
            recovered = EHRRecoveryService(
                db, connector=make_connector(transport=asgi_transport())
            ).reconcile_appointment(self.appt.id)
        finally:
            db.close()
        self.assertEqual(recovered.ehr_sync_status, "synced")
        # The key MUST NOT change between attempts.
        self.assertEqual(recovered.ehr_idempotency_key, key_after_failure)
        self.assertTrue(key_after_failure.startswith("plat-appt-"))
        self.assertEqual(ehr_appt_count(self.appt.id), 1)

    def test_14_key_is_deterministic_per_appointment(self) -> None:
        db = PlatformSession()
        try:
            first = EHRSyncService(db, connector=make_connector(transport=asgi_transport())).sync_appointment(self.appt.id)
            other_appt = self.fixture.book(hh=11)
            second = EHRSyncService(db, connector=make_connector(transport=asgi_transport())).sync_appointment(other_appt.id)
        finally:
            db.close()
        self.assertNotEqual(first.ehr_idempotency_key, second.ehr_idempotency_key)
        self.assertEqual(first.ehr_idempotency_key, f"plat-appt-{self.appt.id}")


class TestDUnknownAdopt(unittest.TestCase):
    """D — timeout with a LANDED write → unknown → reconciliation adopts (15-20)."""

    @classmethod
    def setUpClass(cls) -> None:
        EHRBase.metadata.create_all(ehr_engine)
        seed_ehr_provider(PROVIDER)

    def setUp(self) -> None:
        self.fixture = PlatformFixture()
        self.appt = self.fixture.book()
        connector = make_connector(transport=timeout_after_write_transport(), timeout=0.05)
        db = PlatformSession()
        try:
            synced = EHRSyncService(db, connector=connector).sync_appointment(self.appt.id)
        finally:
            db.close()
        self.assertEqual(synced.ehr_sync_status, "unknown")
        self.assertEqual(last_op(self.appt.id).error_code, "ehr_unknown_outcome")
        self.correlation_id = last_op(self.appt.id).correlation_id

    def test_15_unknown_recorded(self) -> None:
        db = PlatformSession()
        try:
            appt = db.get(PlatformAppointmentModel, self.appt.id)
            self.assertEqual(appt.ehr_sync_status, "unknown")
            self.assertIsNone(appt.external_ehr_appointment_id)
        finally:
            db.close()

    def test_16_ehr_actually_created_despite_timeout(self) -> None:
        self.assertEqual(ehr_appt_count(self.appt.id), 1)

    def test_17_reconciliation_finds_appointment(self) -> None:
        finder = make_connector(transport=asgi_transport())
        found = finder.find_appointment_by_platform_ref(self.appt.id, correlation_id="probe")
        self.assertIsNotNone(found)

    def test_18_adopt_external_id_and_confirm(self) -> None:
        db = PlatformSession()
        try:
            recovered = EHRRecoveryService(db, connector=make_connector(transport=asgi_transport())).reconcile_appointment(self.appt.id)
            self.assertEqual(recovered.ehr_sync_status, "synced")
            self.assertTrue(recovered.external_ehr_appointment_id.startswith("EHR-"))
            self.assertIsNotNone(recovered.ehr_synced_at)
        finally:
            db.close()
        op = last_op(self.appt.id)
        self.assertEqual(op.operation, "ehr.reconcile_appointment")
        self.assertEqual(op.outcome, "success")
        self.assertEqual(op.response_summary["action"], "adopted")

    def test_19_no_duplicate_ehr_appointment(self) -> None:
        db = PlatformSession()
        try:
            EHRRecoveryService(db, connector=make_connector(transport=asgi_transport())).reconcile_appointment(self.appt.id)
        finally:
            db.close()
        self.assertEqual(ehr_appt_count(self.appt.id), 1)

    def test_20_platform_synced_after_reconciliation(self) -> None:
        db = PlatformSession()
        try:
            recovered = EHRRecoveryService(db, connector=make_connector(transport=asgi_transport())).reconcile_appointment(self.appt.id)
            self.assertEqual(recovered.ehr_sync_status, "synced")
        finally:
            db.close()
        # Correlation context preserved from the original failed sync.
        self.assertEqual(last_op(self.appt.id).correlation_id, self.correlation_id)


class TestEUnknownNotFound(unittest.TestCase):
    """E — unknown, but the EHR never processed the write → safe retry (21-26)."""

    @classmethod
    def setUpClass(cls) -> None:
        EHRBase.metadata.create_all(ehr_engine)
        seed_ehr_provider(PROVIDER)

    def setUp(self) -> None:
        self.fixture = PlatformFixture()
        self.appt = self.fixture.book()
        connector = make_connector(transport=lost_everywhere_transport(), timeout=0.05)
        db = PlatformSession()
        try:
            synced = EHRSyncService(db, connector=connector).sync_appointment(self.appt.id)
        finally:
            db.close()
        self.assertEqual(synced.ehr_sync_status, "unknown")
        self.assertEqual(ehr_appt_count(self.appt.id), 0)

    def test_21_22_timeout_unknown_and_nothing_at_ehr(self) -> None:
        finder = make_connector(transport=asgi_transport())
        self.assertIsNone(finder.find_appointment_by_platform_ref(self.appt.id, correlation_id="probe"))

    def test_23_26_safe_retry_same_key_exactly_one_record(self) -> None:
        db = PlatformSession()
        try:
            appt_before = db.get(PlatformAppointmentModel, self.appt.id)
            key_before = appt_before.ehr_idempotency_key
            recovered = EHRRecoveryService(db, connector=make_connector(transport=asgi_transport())).reconcile_appointment(self.appt.id)
        finally:
            db.close()
        self.assertEqual(recovered.ehr_sync_status, "synced")
        self.assertTrue(recovered.external_ehr_appointment_id.startswith("EHR-"))
        self.assertEqual(recovered.ehr_idempotency_key, key_before)  # SAME key

        retry_op = last_op(self.appt.id)
        self.assertEqual(retry_op.operation, "ehr.reconcile_appointment")
        self.assertEqual(retry_op.response_summary["action"], "safe_retry")
        self.assertEqual(ehr_appt_count(self.appt.id), 1)  # exactly one EHR record

        # Final verification: the EHR record itself is fetchable and consistent.
        verifier = make_connector(transport=asgi_transport())
        verified = verifier.get_appointment(recovered.external_ehr_appointment_id, correlation_id="probe")
        self.assertEqual(verified.status, "booked")


class TestFRepeatedRecovery(unittest.TestCase):
    """F — reconciliation is idempotent (27-29)."""

    @classmethod
    def setUpClass(cls) -> None:
        EHRBase.metadata.create_all(ehr_engine)
        seed_ehr_provider(PROVIDER)

    def setUp(self) -> None:
        self.fixture = PlatformFixture()
        self.appt = self.fixture.book()
        connector = make_connector(transport=timeout_after_write_transport(), timeout=0.05)
        db = PlatformSession()
        try:
            EHRSyncService(db, connector=connector).sync_appointment(self.appt.id)
        finally:
            db.close()
        self.recovery_db = PlatformSession()
        self.addCleanup(self.recovery_db.close)
        self.recovery = EHRRecoveryService(
            self.recovery_db, connector=make_connector(transport=asgi_transport())
        )

    def test_27_29_reconcile_twice_is_safe_and_preserves_external_id(self) -> None:
        first = self.recovery.reconcile_appointment(self.appt.id)
        self.assertEqual(first.ehr_sync_status, "synced")
        external_id = first.external_ehr_appointment_id

        second = self.recovery.reconcile_appointment(self.appt.id)
        self.assertEqual(second.external_ehr_appointment_id, external_id)
        self.assertEqual(second.ehr_sync_status, "synced")
        self.assertEqual(ehr_appt_count(self.appt.id), 1)

        # The reconcile log records the first adopt; no second adopt occurred.
        ops = (
            PlatformSession()
            .query(IntegrationOperation)
            .filter(
                IntegrationOperation.resource_id == self.appt.id,
                IntegrationOperation.operation == "ehr.reconcile_appointment",
            )
            .all()
        )
        self.assertEqual(len(ops), 1)

    def test_29b_sweep_only_touches_unknown(self) -> None:
        self.recovery.reconcile_appointment(self.appt.id)
        # A second appointment in FAILED state must not be swept: a sweep
        # adopts unknowns; failed rows need an explicit reconcile (safe retry).
        other = self.fixture.book(hh=11)
        boom = make_connector(
            transport=httpx.MockTransport(lambda request: httpx.Response(500, json={"detail": "boom"}))
        )
        db = PlatformSession()
        try:
            EHRSyncService(db, connector=boom).sync_appointment(other.id)
            recovered = self.recovery.sweep_unknown()
        finally:
            db.close()
        swept_ids = [a.id for a in recovered]
        self.assertNotIn(self.appt.id, swept_ids)  # already synced → not an unknown
        self.assertNotIn(other.id, swept_ids)  # failed → not an unknown
        db = PlatformSession()
        try:
            self.assertEqual(db.get(PlatformAppointmentModel, other.id).ehr_sync_status, "failed")
        finally:
            db.close()

        # Explicit reconciliation of the failed row retries the create safely.
        db = PlatformSession()
        try:
            done = EHRRecoveryService(
                db, connector=make_connector(transport=asgi_transport())
            ).reconcile_appointment(other.id)
        finally:
            db.close()
        self.assertEqual(done.ehr_sync_status, "synced")
        self.assertEqual(ehr_appt_count(other.id), 1)


class TestGSecurity(unittest.TestCase):
    """G — auth + tenant isolation on the recovery endpoints (30-32)."""

    @classmethod
    def setUpClass(cls) -> None:
        EHRBase.metadata.create_all(ehr_engine)
        seed_ehr_provider(PROVIDER)

    def setUp(self) -> None:
        self.fixture = PlatformFixture()
        self.appt = self.fixture.book()

        suffix = uuid.uuid4().hex[:8]
        db = PlatformSession()
        try:
            other = Hospital(name=f"Recovery Hosp {suffix}", status="approved")
            db.add(other)
            db.flush()
            db.add(
                User(
                    email=f"recovery-admin-{suffix}@test.health",
                    password_hash=hash_password("TestPass1!"),
                    full_name="Other Recovery Admin",
                    role="hospital_admin",
                    hospital_id=other.id,
                )
            )
            other_patient_user = User(
                email=f"recovery-patient-{suffix}@test.health",
                password_hash=hash_password("TestPass1!"),
                full_name="Other Recovery Patient",
                role="patient",
            )
            db.add(other_patient_user)
            db.flush()
            db.add(Patient(user_id=other_patient_user.id))
            db.commit()
            self.other_admin_email = f"recovery-admin-{suffix}@test.health"
            self.other_patient_email = f"recovery-patient-{suffix}@test.health"
        finally:
            db.close()

    def _login(self, email: str) -> dict:
        resp = platform_client.post("/auth/login", json={"email": email, "password": "TestPass1!"})
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def _unknown_appt(self) -> None:
        connector = make_connector(transport=lost_everywhere_transport(), timeout=0.05)
        db = PlatformSession()
        try:
            EHRSyncService(db, connector=connector).sync_appointment(self.appt.id)
        finally:
            db.close()

    def test_30_other_hospital_admin_cannot_reconcile(self) -> None:
        self._unknown_appt()
        resp = platform_client.post(
            f"/appointments/{self.appt.id}/reconcile-ehr", headers=self._login(self.other_admin_email)
        )
        self.assertEqual(resp.status_code, 403)

    def test_31_patient_cannot_access_another_patients_appointment(self) -> None:
        self._unknown_appt()
        resp = platform_client.post(
            f"/appointments/{self.appt.id}/reconcile-ehr", headers=self._login(self.other_patient_email)
        )
        self.assertEqual(resp.status_code, 403)

    def test_32_unauthenticated_recovery_rejected(self) -> None:
        self.assertEqual(platform_client.post(f"/appointments/{self.appt.id}/reconcile-ehr").status_code, 401)
        self.assertEqual(platform_client.post("/integrations/reconcile-unknown").status_code, 401)

    def test_32b_owner_patient_can_reconcile_own_appointment(self) -> None:
        self._unknown_appt()
        with mock.patch(
            "backend.app.services.build_ehr_connector", return_value=make_connector(transport=asgi_transport())
        ):
            resp = platform_client.post(
                f"/appointments/{self.appt.id}/reconcile-ehr", headers=self._login(self.fixture.patient_user_email)
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["ehr_sync_status"], "synced")

    def test_32c_hospital_admin_sweep_is_tenant_scoped(self) -> None:
        self._unknown_appt()
        # A foreign unknown appointment at another hospital.
        foreign = PlatformFixture()
        foreign_appt = foreign.book()
        connector = make_connector(transport=lost_everywhere_transport(), timeout=0.05)
        db = PlatformSession()
        try:
            EHRSyncService(db, connector=connector).sync_appointment(foreign_appt.id)
        finally:
            db.close()

        with mock.patch(
            "backend.app.services.build_ehr_connector", return_value=make_connector(transport=asgi_transport())
        ):
            resp = platform_client.post("/integrations/reconcile-unknown", headers=self._login(self.fixture.admin_email))
        self.assertEqual(resp.status_code, 200, resp.text)
        recovered_ids = [row["id"] for row in resp.json()]
        self.assertIn(self.appt.id, recovered_ids)
        self.assertNotIn(foreign_appt.id, recovered_ids)


class TestStateSafety(unittest.TestCase):
    """Appointment/EHR state transition guards (spec §12)."""

    def test_unknown_cannot_become_cancelled(self) -> None:
        self.assertFalse(EHR_SYNC_TRANSITIONS["unknown"] >= {"cancelled"})

    def test_synced_is_terminal(self) -> None:
        self.assertEqual(EHR_SYNC_TRANSITIONS["synced"], {"synced"})

    def test_transition_map_shape(self) -> None:
        for state, targets in EHR_SYNC_TRANSITIONS.items():
            for target in targets:
                self.assertIn(target, (*SYNC_STATUSES,))

    def test_unknown_appointment_cannot_be_cancelled(self) -> None:
        """Spec §12: unknown → cancelled must be impossible. With the EHR
        outcome unresolved, cancellation would strand a live EHR appointment."""
        fixture = PlatformFixture()
        appt = fixture.book()
        db = PlatformSession()
        try:
            connector = make_connector(transport=lost_everywhere_transport(), timeout=0.05)
            EHRSyncService(db, connector=connector).sync_appointment(appt.id)
            with self.assertRaises(SchedulingError) as ctx:
                SchedulingService(db).cancel_appointment(appt.id)
            self.assertEqual(ctx.exception.reason, "ehr_outcome_unknown")
        finally:
            db.close()

    def test_sync_service_refuses_invalid_transition(self) -> None:
        # A synced appointment cannot be pushed back to unknown/failed.
        db = PlatformSession()
        try:
            appt = PlatformAppointmentModel(
                hospital_id="h", patient_id="p", doctor_id="d",
                start_at=datetime(2100, 1, 1, 9), end_at=datetime(2100, 1, 1, 9, 30),
                status="booked", ehr_sync_status="synced",
            )
            service = EHRSyncService(db)
            with self.assertRaises(EHRSyncError):
                service._set_ehr_status(appt, "unknown")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
