"""Phase 2 scheduling tests.

Run from repo root:  python -m unittest discover -s tests -v
(also runs green under pytest if installed:  pytest tests/ -v)

Covers the six required scenarios:
  1. valid slot          2. blocked slot        3. outside availability
  4. already booked      5. inactive doctor     6. concurrent double booking
plus state transitions and tenant-isolated availability via the API.

NOTE: tests share one throwaway file-backed SQLite DB (set before app import)
because the concurrent-booking test genuinely needs two separate connections.
"""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from datetime import date, datetime, time, timedelta

# Throwaway DB BEFORE importing the app. If Phase 1's module already ran in this
# process it set DATABASE_URL first; setdefault keeps us on the same throwaway DB
# (and standalone runs get their own — never the developer's data/platform.db).
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp(prefix='hap-p2-')}/test.db")
os.environ.setdefault("APP_ENVIRONMENT", "test")

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.security import hash_password  # noqa: E402
from backend.app.database.base import SessionLocal  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.models import Appointment, Doctor, DoctorAvailability, Hospital, Patient, User  # noqa: E402
from backend.app.scheduling import (  # noqa: E402
    ConflictError,
    SchedulingService,
    ValidationError,
)

client = TestClient(app)


def future_weekday(weekday: int, min_days_ahead: int = 35) -> date:
    """A date >= min_days_ahead out that falls on the given weekday (0=Mon)."""
    day = date.today() + timedelta(days=min_days_ahead)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day  # comfortably inside the 60-day booking horizon


def at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm))


class SchedulingTestBase(unittest.TestCase):
    """Creates an approved hospital + fresh active doctor + Mon 09:00-12:00 availability."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.service = SchedulingService(SessionLocal())

    def setUp(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            hospital = Hospital(
                name=f"Test Hosp {suffix}", city="Testville", status="approved"
            )
            db.add(hospital)
            db.flush()
            self.hospital_id = hospital.id

            admin = User(
                email=f"admin-{suffix}@test.health",
                password_hash=hash_password("TestPass1!"),
                full_name="Admin",
                role="hospital_admin",
                hospital_id=hospital.id,
            )
            db.add(admin)
            db.flush()
            self.admin_user_id = admin.id

            doctor = Doctor(
                hospital_id=hospital.id,
                full_name=f"Dr. Test {suffix}",
                specialty="orthopedics",
                consultation_minutes=30,
                appointment_types=["in_person", "video"],
                status="active",
            )
            db.add(doctor)
            db.flush()
            self.doctor_id = doctor.id

            availability = DoctorAvailability(
                hospital_id=hospital.id,
                doctor_id=doctor.id,
                weekday=0,  # Monday
                start_time="09:00",
                end_time="12:00",
                slot_minutes=30,
                appointment_types=["in_person", "video"],
            )
            db.add(availability)
            db.flush()
            self.availability_id = availability.id

            # Two patients with profiles, for booking tests.
            self.patient_ids = []
            for n in (1, 2):
                user = User(
                    email=f"patient{n}-{suffix}@test.health",
                    password_hash=hash_password("TestPass1!"),
                    full_name=f"Patient {n}",
                    role="patient",
                )
                db.add(user)
                db.flush()
                patient = Patient(user_id=user.id)
                db.add(patient)
                db.flush()
                self.patient_ids.append(patient.id)

            self.monday = future_weekday(0)
            db.commit()
        finally:
            db.close()

    # -- helpers -------------------------------------------------------

    def book(self, patient_idx: int, day: date, hh: int, mm: int = 0, **kwargs) -> Appointment:
        session = SessionLocal()
        svc = SchedulingService(session)
        try:
            return svc.book_appointment(
                patient_id=self.patient_ids[patient_idx],
                doctor_id=self.doctor_id,
                start_at=at(day, hh, mm),
                **kwargs,
            )
        finally:
            session.close()

    def slots_for(self, day: date) -> list[dict]:
        session = SessionLocal()
        try:
            result = SchedulingService(session).search_availability(self.doctor_id, day, day)
        finally:
            session.close()
        assert result["days"], "expected one day in search result"
        return result["days"][0]["slots"]

    def blocking_count(self, day: date, hh: int, mm: int = 0) -> int:
        session = SessionLocal()
        try:
            return len(
                session.query(Appointment)
                .filter(
                    Appointment.doctor_id == self.doctor_id,
                    Appointment.start_at == at(day, hh, mm),
                    Appointment.status.in_(("booked", "pending_external")),
                )
                .all()
            )
        finally:
            session.close()


class TestValidSlot(SchedulingTestBase):
    """Test 1 — active doctor + availability returns valid slots."""

    def test_six_thirty_minute_slots_returned(self) -> None:
        slots = self.slots_for(self.monday)
        starts = [datetime.fromisoformat(s["start_at"]).strftime("%H:%M") for s in slots]
        # 09:00..11:30 inclusive, per the PRD example.
        self.assertEqual(starts, ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"])
        self.assertTrue(all(s["available"] for s in slots))
        self.assertTrue(all(s["reason"] is None for s in slots))

    def test_last_slot_fits_window(self) -> None:
        slots = self.slots_for(self.monday)
        last = datetime.fromisoformat(slots[-1]["end_at"])
        self.assertEqual(last.strftime("%H:%M"), "12:00")  # 11:30 + 30 == window end


class TestBlockedSlot(SchedulingTestBase):
    """Test 2 — blocked periods remove exactly the covered slots."""

    def _block(self, day: date, start_hhmm: str, end_hhmm: str, kind: str = "blocked") -> None:
        session = SessionLocal()
        try:
            from backend.app.models import BlockedPeriod

            db = session
            db.add(
                BlockedPeriod(
                    hospital_id=self.hospital_id,
                    doctor_id=self.doctor_id,
                    kind=kind,
                    reason="test",
                    start_at=datetime.combine(day, time.fromisoformat(start_hhmm)),
                    end_at=datetime.combine(day, time.fromisoformat(end_hhmm)),
                )
            )
            db.commit()
        finally:
            session.close()

    def test_full_slot_block(self) -> None:
        self._block(self.monday, "10:00", "11:00")
        slots = {s["start_at"][11:16]: s for s in self.slots_for(self.monday)}
        self.assertFalse(slots["10:00"]["available"])
        self.assertEqual(slots["10:00"]["reason"], "slot_blocked")
        self.assertFalse(slots["10:30"]["available"])
        # Other slots remain available
        for t in ("09:00", "09:30", "11:00", "11:30"):
            self.assertTrue(slots[t]["available"], f"{t} should be available")

    def test_partial_overlap_block(self) -> None:
        # Block 10:15-10:45: touches both 10:00 and 10:30 slots
        self._block(self.monday, "10:15", "10:45")
        slots = {s["start_at"][11:16]: s for s in self.slots_for(self.monday)}
        self.assertFalse(slots["10:00"]["available"])
        self.assertFalse(slots["10:30"]["available"])
        self.assertTrue(slots["09:30"]["available"])
        self.assertTrue(slots["11:00"]["available"])

    def test_leave_kind_reports_doctor_on_leave(self) -> None:
        self._block(self.monday, "09:00", "10:00", kind="leave")
        slots = {s["start_at"][11:16]: s for s in self.slots_for(self.monday)}
        self.assertEqual(slots["09:00"]["reason"], "doctor_on_leave")
        self.assertTrue(slots["10:00"]["available"])

    def test_blocked_slot_rejected_at_booking(self) -> None:
        self._block(self.monday, "10:00", "11:00")
        with self.assertRaises(ConflictError) as ctx:
            self.book(0, self.monday, 10, 0)
        self.assertEqual(ctx.exception.reason, "slot_blocked")


class TestOutsideAvailability(SchedulingTestBase):
    """Test 3 — times outside configured availability are never offered or accepted."""

    def test_1300_not_in_search_results(self) -> None:
        slots = self.slots_for(self.monday)
        starts = [datetime.fromisoformat(s["start_at"]).strftime("%H:%M") for s in slots]
        self.assertNotIn("13:00", starts)
        self.assertNotIn("12:00", starts)
        self.assertNotIn("08:30", starts)

    def test_booking_outside_hours_rejected(self) -> None:
        with self.assertRaises(ConflictError) as ctx:
            self.book(0, self.monday, 13, 0)
        self.assertEqual(ctx.exception.reason, "outside_working_hours")

    def test_off_grid_slot_rejected(self) -> None:
        # 09:15 is inside the window but not on the 30-minute grid.
        with self.assertRaises(ConflictError) as ctx:
            self.book(0, self.monday, 9, 15)
        self.assertEqual(ctx.exception.reason, "outside_working_hours")

    def test_day_without_availability_rejected(self) -> None:
        tuesday = future_weekday(1)
        with self.assertRaises(ConflictError) as ctx:
            self.book(0, tuesday, 9, 0)
        self.assertEqual(ctx.exception.reason, "no_availability_configured")

    def test_past_slot_rejected(self) -> None:
        yesterday = date.today() - timedelta(days=1)
        while yesterday.weekday() != 0:
            yesterday -= timedelta(days=1)
        with self.assertRaises(ValidationError) as ctx:
            self.book(0, yesterday, 9, 0)
        self.assertEqual(ctx.exception.reason, "in_past")


class TestAlreadyBooked(SchedulingTestBase):
    """Test 4 — a booked slot disappears from availability and rejects re-booking."""

    def test_second_booking_rejected(self) -> None:
        self.book(0, self.monday, 9, 0)  # Patient A books Monday 09:00
        with self.assertRaises(ConflictError) as ctx:
            self.book(1, self.monday, 9, 0)  # Patient B: same slot -> rejected
        self.assertEqual(ctx.exception.reason, "slot_already_booked")
        self.assertEqual(self.blocking_count(self.monday, 9), 1)

    def test_slot_blocked_after_booking(self) -> None:
        self.book(0, self.monday, 10, 0)
        slots = {s["start_at"][11:16]: s for s in self.slots_for(self.monday)}
        self.assertFalse(slots["10:00"]["available"])
        self.assertEqual(slots["10:00"]["reason"], "slot_already_booked")
        # 10:30 is free (appointments don't bleed into neighbours)
        self.assertTrue(slots["10:30"]["available"])

    def test_rebooking_same_slot_fails(self) -> None:
        self.book(0, self.monday, 10, 0)
        with self.assertRaises(ConflictError) as ctx:
            self.book(1, self.monday, 10, 0)
        self.assertEqual(ctx.exception.reason, "slot_already_booked")
        self.assertEqual(self.blocking_count(self.monday, 10), 1)


class TestInactiveDoctor(SchedulingTestBase):
    """Test 5 — inactive doctors expose no slots and reject bookings."""

    def _set_inactive(self) -> None:
        session = SessionLocal()
        try:
            session.get(Doctor, self.doctor_id).status = "inactive"
            session.commit()
        finally:
            session.close()

    def test_no_slots_returned(self) -> None:
        self._set_inactive()
        session = SessionLocal()
        try:
            result = SchedulingService(session).search_availability(self.doctor_id, self.monday, self.monday)
        finally:
            session.close()
        self.assertEqual(result["days"][0]["reason"], "doctor_inactive")
        self.assertEqual(result["days"][0]["slots"], [])

    def test_booking_rejected(self) -> None:
        self._set_inactive()
        with self.assertRaises(ConflictError) as ctx:
            self.book(0, self.monday, 10, 0)
        self.assertEqual(ctx.exception.reason, "doctor_inactive")


class TestConcurrentDoubleBooking(SchedulingTestBase):
    """Test 6 — two patients racing for the same slot: exactly one wins."""

    def test_interleaved_transactions_only_one_wins(self) -> None:
        """Patient A validates + inserts (uncommitted); Patient B validates
        against the *same* visible state (also passes — the realistic race),
        then B's insert hits the write lock. Exactly one booking survives."""
        slot = at(self.monday, 10, 0)

        session_a = SessionLocal()
        session_b = SessionLocal()
        try:
            svc_a = SchedulingService(session_a)
            svc_b = SchedulingService(session_b)

            # Both revalidate concurrently — both see the slot free.
            svc_a.validate_slot(self.doctor_id, slot)
            svc_b.validate_slot(self.doctor_id, slot)

            # A inserts but does NOT commit yet (holds the SQLite write lock).
            session_a.add(
                Appointment(
                    hospital_id=self.hospital_id,
                    patient_id=self.patient_ids[0],
                    doctor_id=self.doctor_id,
                    appointment_type="in_person",
                    start_at=slot,
                    end_at=slot + timedelta(minutes=30),
                    status="booked",
                )
            )
            session_a.flush()

            # B, having passed validation, tries to insert too.
            session_b.add(
                Appointment(
                    hospital_id=self.hospital_id,
                    patient_id=self.patient_ids[1],
                    doctor_id=self.doctor_id,
                    appointment_type="in_person",
                    start_at=slot,
                    end_at=slot + timedelta(minutes=30),
                    status="booked",
                )
            )
            with self.assertRaises(Exception) as ctx:
                session_b.flush()
            self.assertIn(
                type(ctx.exception).__name__, {"OperationalError", "IntegrityError"},
                f"expected a database-level rejection, got {ctx.exception}",
            )
            session_b.rollback()

            # A commits — its booking wins.
            session_a.commit()
        finally:
            session_a.close()
            session_b.close()

        self.assertEqual(self.blocking_count(self.monday, 10), 1)

    def test_database_unique_index_rejects_second_active_booking(self) -> None:
        """Even if application validation were bypassed entirely, the partial
        unique index makes a second active booking for the slot impossible."""
        from sqlalchemy.exc import IntegrityError

        slot = at(self.monday, 11, 0)
        self.book(0, self.monday, 11, 0)  # committed booking via the service

        session = SessionLocal()
        try:
            session.add(
                Appointment(
                    hospital_id=self.hospital_id,
                    patient_id=self.patient_ids[1],
                    doctor_id=self.doctor_id,
                    appointment_type="in_person",
                    start_at=slot,
                    end_at=slot + timedelta(minutes=30),
                    status="booked",
                )
            )
            with self.assertRaises(IntegrityError):
                session.flush()
            session.rollback()
        finally:
            session.close()

        self.assertEqual(self.blocking_count(self.monday, 11), 1)

    def test_cancelled_appointment_releases_slot(self) -> None:
        appt = self.book(0, self.monday, 10, 0)
        session = SessionLocal()
        try:
            SchedulingService(session).cancel_appointment(appt.id)
        finally:
            session.close()
        slots = {s["start_at"][11:16]: s for s in self.slots_for(self.monday)}
        self.assertTrue(slots["10:00"]["available"])


class TestStateTransitionsAndQueries(SchedulingTestBase):
    def test_cancel_and_invalid_retransition(self) -> None:
        appt = self.book(0, self.monday, 9, 0)
        session = SessionLocal()
        try:
            svc = SchedulingService(session)
            cancelled = svc.cancel_appointment(appt.id)
            self.assertEqual(cancelled.status, "cancelled")
            with self.assertRaises(ConflictError) as ctx:
                svc.cancel_appointment(appt.id)
            self.assertEqual(ctx.exception.reason, "invalid_transition")
        finally:
            session.close()

    def test_patient_listing_scoped_to_own_appointments(self) -> None:
        appt = self.book(0, self.monday, 9, 0)
        session = SessionLocal()
        try:
            svc = SchedulingService(session)
            mine = svc.list_appointments(patient_id=self.patient_ids[0])
            self.assertEqual([a.id for a in mine], [appt.id])
        finally:
            session.close()

    def test_tenant_isolated_appointment_lookup(self) -> None:
        appt = self.book(0, self.monday, 9, 0)
        session = SessionLocal()
        try:
            svc = SchedulingService(session)
            with self.assertRaises(Exception):
                svc.get_appointment(appt.id, hospital_id="some-other-hospital")
        finally:
            session.close()


class TestAvailabilityAPI(unittest.TestCase):
    """HTTP layer: availability endpoint scope rules (tenant isolation)."""

    @classmethod
    def setUpClass(cls) -> None:
        suffix = uuid.uuid4().hex[:8]
        db = SessionLocal()
        try:
            hosp_a = Hospital(name=f"API Hosp A {suffix}", status="approved")
            hosp_b = Hospital(name=f"API Hosp B {suffix}", status="approved")
            db.add_all([hosp_a, hosp_b])
            db.flush()
            cls.doctor_a = Doctor(
                hospital_id=hosp_a.id, full_name=f"Dr. API {suffix}",
                specialty="orthopedics", status="active",
            )
            db.add(cls.doctor_a)
            db.flush()
            db.add(
                DoctorAvailability(
                    hospital_id=hosp_a.id, doctor_id=cls.doctor_a.id,
                    weekday=0, start_time="09:00", end_time="12:00", slot_minutes=30,
                )
            )
            for name, hosp in (("a", hosp_a), ("b", hosp_b)):
                db.add(
                    User(
                        email=f"api-admin-{name}-{suffix}@test.health",
                        password_hash=hash_password("TestPass1!"),
                        full_name=f"Admin {name}", role="hospital_admin", hospital_id=hosp.id,
                    )
                )
            db.commit()
            cls.suffix = suffix
        finally:
            db.close()

    def _login(self, which: str) -> str:
        resp = client.post(
            "/auth/login",
            json={
                "email": f"api-admin-{which}-{self.suffix}@test.health",
                "password": "TestPass1!",
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["access_token"]

    def test_availability_requires_auth(self) -> None:
        resp = client.get(f"/doctors/{self.doctor_a.id}/availability")
        self.assertEqual(resp.status_code, 401)

    def test_patient_can_search_any_approved_hospital(self) -> None:
        resp = client.post(
            "/auth/register-patient",
            json={"email": f"api-pat-{self.suffix}@test.health",
                  "password": "TestPass1!", "full_name": "P"},
        )
        assert resp.status_code == 201, resp.text
        token = resp.json()["access_token"]
        monday = future_weekday(0)
        resp = client.get(
            f"/doctors/{self.doctor_a.id}/availability",
            params={"from_date": monday.isoformat(), "to_date": monday.isoformat()},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(len(body["days"]), 1)
        self.assertTrue(all(s["available"] for s in body["days"][0]["slots"]))

    def test_other_hospital_admin_cannot_see_doctor(self) -> None:
        token_b = self._login("b")
        monday = future_weekday(0)
        resp = client.get(
            f"/doctors/{self.doctor_a.id}/availability",
            params={"from_date": monday.isoformat(), "to_date": monday.isoformat()},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        # Tenant isolation: existence hidden behind 404 (handled by the
        # SchedulingError -> HTTP mapper registered in main.py).
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
