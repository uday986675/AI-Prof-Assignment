"""Phase 6 — pre-visit questionnaires: assignment, conversational collection,
doctor view.

Run from repo root:  python -m unittest discover -s tests -v

Covers:
  A. Template management (create/list, validation, tenant scoping, duplicate)
  B. Auto-assignment on booking (REST + agent capability path, idempotent)
  C. Answer submission (conversational one-at-a-time + batch) with validation
  D. Completion gate (mandatory answers, double-complete, closed forms)
  E. Doctor view (structured answers, scoping)
  F. Cancellation propagation (pending forms cancelled with the appointment)
  G. Agent conversational collection (graph turn, chaining, invalid answers)
  H. RBAC / tenant isolation / auth
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock  # noqa: F401  (LLM patching in TestG)
import uuid
from datetime import date

# Throwaway DBs BEFORE any app import (mirrors the Phase 3/4/5 harness).
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp(prefix='hap-p6-')}/platform.db")
os.environ.setdefault("MOCK_EHR_DATABASE_URL", f"sqlite:///{tempfile.mkdtemp(prefix='hap-p6-')}/ehr.db")
os.environ.setdefault("MOCK_EHR_API_KEY", "test-ehr-key-6")
os.environ.setdefault("APP_ENVIRONMENT", "test")

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.security import hash_password  # noqa: E402
from backend.app.database.base import SessionLocal as PlatformSession  # noqa: E402
from backend.app.main import app as platform_app  # noqa: E402
from backend.app.models import (  # noqa: E402
    Appointment,
    Doctor,
    DoctorAvailability,
    Hospital,
    Patient,
    QuestionnaireAssignment,
    QuestionnaireTemplate,
    User,
)
from backend.app.scheduling import SchedulingService, now_utc  # noqa: E402
from datetime import datetime, time, timedelta  # noqa: E402

platform_client = TestClient(platform_app)

# Force the deterministic (no-LLM) interpreter path for every test: Phase 6
# tests exercise questionnaire logic, not the LLM.
import backend.app.agent.llm as agent_llm  # noqa: E402
from backend.app.agent.graph import run_turn  # noqa: E402
from backend.app.agent.state import ConversationState  # noqa: E402


def _no_llm(*args, **kwargs):  # pragma: no cover - simple stub
    raise agent_llm.LLMUnavailableError("tests run without an LLM")


def login(email: str, password: str = "TestPass1!") -> str:
    response = platform_client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm))


def future_weekday(weekday: int, min_days_ahead: int = 35) -> date:
    day = now_utc().date() + timedelta(days=min_days_ahead)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day


class QFixture:
    """Approved hospital + doctor + patient (+ a second tenant for isolation)."""

    def __init__(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.suffix = suffix
        db = PlatformSession()
        try:
            hospital = Hospital(name=f"Q Hosp {suffix}", status="approved")
            db.add(hospital)
            db.flush()
            self.hospital_id = hospital.id

            other = Hospital(name=f"Q Other {suffix}", status="approved")
            db.add(other)
            db.flush()
            self.other_hospital_id = other.id

            self.admin_email = f"q-admin-{suffix}@test.health"
            db.add(User(email=self.admin_email, password_hash=hash_password("TestPass1!"),
                        full_name="Q Admin", role="hospital_admin", hospital_id=hospital.id))
            db.flush()

            doctor_user = User(email=f"q-dr-{suffix}@test.health", password_hash=hash_password("TestPass1!"),
                               full_name="Dr. Q Test", role="doctor", hospital_id=hospital.id)
            db.add(doctor_user)
            db.flush()
            doctor = Doctor(hospital_id=hospital.id, full_name=f"Dr. A-Q {suffix}", specialty="orthopedics",
                            consultation_minutes=30, appointment_types=["in_person", "video"],
                            status="active", external_provider_id="EHR-PROV-Q1", user_id=doctor_user.id)
            db.add(doctor)
            db.flush()
            self.doctor_id = doctor.id

            db.add(DoctorAvailability(hospital_id=hospital.id, doctor_id=doctor.id, weekday=0,
                                      start_time="09:00", end_time="12:00", slot_minutes=30,
                                      appointment_types=["in_person", "video"]))

            patient_user = User(email=f"q-patient-{suffix}@test.health", password_hash=hash_password("TestPass1!"),
                                full_name="Q Patient", role="patient")
            db.add(patient_user)
            db.flush()
            patient = Patient(user_id=patient_user.id, phone="+91 90000 00000", date_of_birth=date(1990, 1, 1))
            db.add(patient)
            db.flush()
            self.patient_email = patient_user.email
            self.patient_id = patient.id

            # second patient, same hospital (privacy boundary)
            other_patient_user = User(email=f"q-p2-{suffix}@test.health", password_hash=hash_password("TestPass1!"),
                                      full_name="Q Patient Two", role="patient")
            db.add(other_patient_user)
            db.flush()
            p2 = Patient(user_id=other_patient_user.id, phone="+91 90000 00001", date_of_birth=date(1985, 2, 2))
            db.add(p2)
            db.flush()
            self.patient2_email = other_patient_user.email
            self.patient2_id = p2.id

            self.monday = future_weekday(0)
            db.commit()
        finally:
            db.close()

    def seed_templates(self) -> tuple[str, str]:
        """Standard + specialty template for the fixture hospital; returns ids."""
        db = PlatformSession()
        try:
            standard = QuestionnaireTemplate(
                hospital_id=self.hospital_id, name=f"Standard {self.suffix}", kind="standard",
                description="Standard intake",
                questions=[
                    {"key": "reason", "kind": "free_text", "prompt": "Main reason for the visit?", "required": True},
                    {"key": "fever", "kind": "boolean", "prompt": "Any fever?", "required": True},
                    {"key": "severity", "kind": "scale_1_10", "prompt": "Pain scale 1-10", "required": True},
                    {"key": "notes", "kind": "free_text", "prompt": "Anything else?", "required": False},
                ],
            )
            specialty = QuestionnaireTemplate(
                hospital_id=self.hospital_id, name=f"orthopedics {self.suffix}", kind="specialty",
                description="Orthopedics intake",
                questions=[
                    {"key": "joint", "kind": "single_choice", "prompt": "Which joint?",
                     "options": ["Shoulder", "Knee", "Back"], "required": True},
                    {"key": "injury", "kind": "boolean", "prompt": "Injury-related?", "required": False},
                ],
            )
            db.add_all([standard, specialty])
            db.commit()
            return standard.id, specialty.id
        finally:
            db.close()

    def book(self, hh: int = 10, mm: int = 0) -> Appointment:
        db = PlatformSession()
        try:
            appt = SchedulingService(db).book_appointment(
                patient_id=self.patient_id, doctor_id=self.doctor_id, start_at=at(self.monday, hh, mm),
            )
            db.expunge_all()
            return appt
        finally:
            db.close()


class TestATemplates(unittest.TestCase):
    """A. Template management (hospital admin, tenant-owned)."""

    def setUp(self) -> None:
        # A platform admin for RBAC checks (matches the Phase 1 harness idiom).
        db = PlatformSession()
        try:
            if not db.query(User).filter(User.email == "platform.admin@test.health").first():
                db.add(User(email="platform.admin@test.health", password_hash=hash_password("AdminPass1!"),
                            full_name="Platform Admin", role="platform_admin"))
                db.commit()
        finally:
            db.close()
        self.fx = QFixture()
        self.fx.seed_templates()
        self.admin = login(self.fx.admin_email)

    def test_1_create_and_list_templates(self) -> None:
        response = platform_client.post(
            "/questionnaires/templates",
            json={
                "name": f"Cardio {self.fx.suffix}",
                "kind": "specialty",
                "questions": [
                    {"key": "chest_pain", "kind": "boolean", "prompt": "Chest pain?", "required": True},
                ],
            },
            headers={"Authorization": f"Bearer {self.admin}"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["kind"], "specialty")
        self.assertEqual(body["questions"][0]["key"], "chest_pain")

        listing = platform_client.get("/questionnaires/templates", headers={"Authorization": f"Bearer {self.admin}"})
        self.assertEqual(listing.status_code, 200)
        names = [t["name"] for t in listing.json()]
        self.assertIn(f"Cardio {self.fx.suffix}", names)
        # tenant scope: only own templates
        self.assertFalse(any(self.fx.suffix not in n for n in names))

    def test_2_template_validation(self) -> None:
        # empty questions
        response = platform_client.post(
            "/questionnaires/templates", json={"name": "Empty", "questions": []},
            headers={"Authorization": f"Bearer {self.admin}"},
        )
        self.assertEqual(response.status_code, 422)
        # choice question without options
        response = platform_client.post(
            "/questionnaires/templates",
            json={"name": "NoOptions", "questions": [{"key": "x", "kind": "single_choice", "prompt": "p"}]},
            headers={"Authorization": f"Bearer {self.admin}"},
        )
        self.assertEqual(response.status_code, 422)
        # duplicate question keys
        response = platform_client.post(
            "/questionnaires/templates",
            json={"name": "DupKeys", "questions": [
                {"key": "x", "kind": "free_text", "prompt": "a"},
                {"key": "x", "kind": "free_text", "prompt": "b"},
            ]},
            headers={"Authorization": f"Bearer {self.admin}"},
        )
        self.assertEqual(response.status_code, 422)

    def test_3_duplicate_template_name_conflicts(self) -> None:
        response = platform_client.post(
            "/questionnaires/templates",
            json={"name": f"Standard {self.fx.suffix}",
                  "questions": [{"key": "x", "kind": "free_text", "prompt": "p"}]},
            headers={"Authorization": f"Bearer {self.admin}"},
        )
        self.assertEqual(response.status_code, 409)

    def test_4_platform_admin_cannot_create_templates(self) -> None:
        # platform admin is not a hospital admin → template endpoints are 403
        plat = login("platform.admin@test.health", "AdminPass1!")
        response = platform_client.get(
            "/questionnaires/templates", headers={"Authorization": f"Bearer {plat}"}
        )
        self.assertEqual(response.status_code, 403)
        # unauthenticated
        response = platform_client.get("/questionnaires/templates")
        self.assertEqual(response.status_code, 401)


class TestBAutoAssignment(unittest.TestCase):
    """B. Auto-assignment on booking — REST and agent paths, idempotent."""

    def setUp(self) -> None:
        self.fx = QFixture()
        self.standard_id, self.specialty_id = self.fx.seed_templates()

    def test_5_rest_booking_auto_assigns(self) -> None:
        token = login(self.fx.patient_email)
        response = platform_client.post(
            "/appointments",
            json={"doctor_id": self.fx.doctor_id, "start_at": at(self.fx.monday, 10, 0).isoformat(),
                  "appointment_type": "in_person"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        appt_id = response.json()["id"]

        listing = platform_client.get(
            f"/questionnaires/appointments/{appt_id}/questionnaires",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(listing.status_code, 200)
        kinds = {a["kind"] for a in listing.json()}
        self.assertEqual(kinds, {"standard", "specialty"})
        # exactly two assignments: one per template
        self.assertEqual(len(listing.json()), 2)
        names = {a["template_name"] for a in listing.json()}
        self.assertIn(f"orthopedics {self.fx.suffix}", names)

    def test_6_agent_booking_auto_assigns(self) -> None:
        from backend.app.agent import capabilities

        db = PlatformSession()
        try:
            user = db.query(User).filter(User.email == self.fx.patient_email).first()
            from backend.app.scheduling import SchedulingService
            patient = SchedulingService(db).ensure_patient_profile(user)
            doctor = db.get(Doctor, self.fx.doctor_id)
            result = capabilities.book_appointment(
                db, user, doctor_id=doctor.id,
                start_at=at(self.fx.monday, 10, 30).isoformat(), appointment_type="in_person",
            )
            appt_id = result["appointment_id"]
            capabilities.start_questionnaire(db, user, appt_id)
        finally:
            db.close()

        db = PlatformSession()
        try:
            pending = capabilities.next_pending_questionnaire(db, user, appt_id)
        finally:
            db.close()
        self.assertIsNotNone(pending)
        self.assertIn("question", pending)

    def test_7_assignment_is_idempotent(self) -> None:
        appt = self.fx.book()
        token = login(self.fx.patient_email)
        first = platform_client.post(
            f"/questionnaires/appointments/{appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(first.status_code, 201)
        second = platform_client.post(
            f"/questionnaires/appointments/{appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(second.status_code, 201)
        db = PlatformSession()
        try:
            count = db.query(QuestionnaireAssignment).filter(
                QuestionnaireAssignment.appointment_id == appt.id
            ).count()
            self.assertEqual(count, 2)  # exactly one per template — no duplicates
        finally:
            db.close()

    def test_8_assignment_scoping(self) -> None:
        # patient of another hospital cannot see/assign
        other = QFixture()
        other.seed_templates()
        appt = self.fx.book()
        token = login(other.patient_email)
        response = platform_client.post(
            f"/questionnaires/appointments/{appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertIn(response.status_code, (403, 404))


class TestCAnswers(unittest.TestCase):
    """C. Answer validation and storage (conversational + batch)."""

    def setUp(self) -> None:
        self.fx = QFixture()
        self.fx.seed_templates()
        self.appt = self.fx.book()
        token = login(self.fx.patient_email)
        self.patient = token
        platform_client.post(
            f"/questionnaires/appointments/{self.appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        assignment = self._first_assignment()
        self.assignment_id = assignment["id"]
        self.questions = {q["key"]: q for q in assignment["questions"]}

    def _first_assignment(self) -> dict:
        listing = platform_client.get(
            f"/questionnaires/appointments/{self.appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        rows = listing.json()
        rows.sort(key=lambda a: a["created_at"] or "")
        standard = [a for a in rows if a["kind"] == "standard"]
        return (standard or rows)[0]

    def test_9_conversational_answers_accepted(self) -> None:
        # free text
        response = platform_client.post(
            f"/questionnaires/assignments/{self.assignment_id}/answers",
            json={"key": "reason", "value": "shoulder pain"},
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["answers"]["reason"], "shoulder pain")
        # boolean as text (conversational coercion)
        response = platform_client.post(
            f"/questionnaires/assignments/{self.assignment_id}/answers",
            json={"key": "fever", "value": "yes"},
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["answers"]["fever"], True)
        # scale
        response = platform_client.post(
            f"/questionnaires/assignments/{self.assignment_id}/answers",
            json={"key": "severity", "value": 7},
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answers"]["severity"], 7)

    def test_10_invalid_answers_rejected(self) -> None:
        cases = [
            ({"key": "fever", "value": "maybe"}, 422),        # bad boolean
            ({"key": "severity", "value": 11}, 422),          # out of range
            ({"key": "severity", "value": "high"}, 422),      # non-numeric
            ({"key": "nope", "value": "x"}, 422),             # unknown question
            ({"key": "reason", "value": ""}, 422),            # required empty
        ]
        for payload, expected in cases:
            response = platform_client.post(
                f"/questionnaires/assignments/{self.assignment_id}/answers",
                json=payload,
                headers={"Authorization": f"Bearer {self.patient}"},
            )
            self.assertEqual(response.status_code, expected, f"{payload}: {response.text}")

    def test_11_batch_answers_and_unknown_keys(self) -> None:
        response = platform_client.post(
            f"/questionnaires/assignments/{self.assignment_id}/answers:batch",
            json={"answers": {"reason": "checkup", "notes": "none"}},
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        answers = response.json()["answers"]
        self.assertEqual(answers["reason"], "checkup")
        self.assertEqual(answers["notes"], "none")

        response = platform_client.post(
            f"/questionnaires/assignments/{self.assignment_id}/answers:batch",
            json={"answers": {"ghost": "x"}},
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        self.assertEqual(response.status_code, 422)

    def test_12_other_patient_cannot_answer(self) -> None:
        other = login(self.fx.patient2_email)
        response = platform_client.post(
            f"/questionnaires/assignments/{self.assignment_id}/answers",
            json={"key": "reason", "value": "snoop"},
            headers={"Authorization": f"Bearer {other}"},
        )
        self.assertEqual(response.status_code, 404)  # existence hidden


class TestDCompletion(unittest.TestCase):
    """D. Completion gate."""

    def setUp(self) -> None:
        self.fx = QFixture()
        self.fx.seed_templates()
        self.appt = self.fx.book(hh=11, mm=0)
        self.patient = login(self.fx.patient_email)
        platform_client.post(
            f"/questionnaires/appointments/{self.appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        listing = platform_client.get(
            f"/questionnaires/appointments/{self.appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        rows = sorted(listing.json(), key=lambda a: a["created_at"] or "")
        standard = [a for a in rows if a["kind"] == "standard"]
        self.assignment_id = (standard or rows)[0]["id"]

    def _answer(self, key: str, value) -> None:
        response = platform_client.post(
            f"/questionnaires/assignments/{self.assignment_id}/answers",
            json={"key": key, "value": value},
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_13_complete_requires_mandatory_answers(self) -> None:
        response = platform_client.post(
            f"/questionnaires/assignments/{self.assignment_id}/complete",
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("reason", response.text)

    def test_14_complete_after_all_mandatory(self) -> None:
        self._answer("reason", "shoulder")
        self._answer("fever", "no")
        self._answer("severity", 4)
        response = platform_client.post(
            f"/questionnaires/assignments/{self.assignment_id}/complete",
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "completed")
        self.assertIsNotNone(response.json()["completed_at"])

        # completed form is closed for further edits
        response = platform_client.post(
            f"/questionnaires/assignments/{self.assignment_id}/answers",
            json={"key": "reason", "value": "change"},
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        self.assertEqual(response.status_code, 409)

    def test_15_optional_questions_do_not_block_completion(self) -> None:
        self._answer("reason", "x")
        self._answer("fever", "no")
        self._answer("severity", 2)
        # 'notes' is optional and unanswered — completion succeeds.
        response = platform_client.post(
            f"/questionnaires/assignments/{self.assignment_id}/complete",
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        self.assertEqual(response.status_code, 200)


class TestEDoctorView(unittest.TestCase):
    """E. Doctor view of structured answers."""

    def setUp(self) -> None:
        self.fx = QFixture()
        self.fx.seed_templates()
        self.appt = self.fx.book(hh=11, mm=30)
        self.patient = login(self.fx.patient_email)
        platform_client.post(
            f"/questionnaires/appointments/{self.appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        listing = platform_client.get(
            f"/questionnaires/appointments/{self.appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {self.patient}"},
        )
        rows = sorted(listing.json(), key=lambda a: a["created_at"] or "")
        standard = [a for a in rows if a["kind"] == "standard"]
        self.assignment_id = (standard or rows)[0]["id"]
        platform_client.post(
            f"/questionnaires/assignments/{self.assignment_id}/answers:batch",
            json={"answers": {"reason": "shoulder pain", "fever": "no", "severity": 6}},
            headers={"Authorization": f"Bearer {self.patient}"},
        )

    def test_16_doctor_sees_structured_answers(self) -> None:
        doctor = login(self.fx.admin_email)  # hospital admin sees tenant assignments too
        response = platform_client.get(
            f"/questionnaires/assignments/{self.assignment_id}",
            headers={"Authorization": f"Bearer {doctor}"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["answers"]["reason"], "shoulder pain")
        self.assertEqual(body["answers"]["severity"], 6)

    def test_17_doctor_mine_listing(self) -> None:
        doctor = login(f"q-dr-{self.fx.suffix}@test.health")
        response = platform_client.get(
            "/questionnaires/doctor/mine?status=pending",
            headers={"Authorization": f"Bearer {doctor}"},
        )
        self.assertEqual(response.status_code, 200)
        ids = [a["id"] for a in response.json()]
        self.assertIn(self.assignment_id, ids)

    def test_18_doctor_cannot_see_other_tenant(self) -> None:
        other = QFixture()
        other.seed_templates()
        doctor = login(f"q-dr-{self.fx.suffix}@test.health")
        other_appt = other.book()
        other_patient = login(other.patient_email)
        platform_client.post(
            f"/questionnaires/appointments/{other_appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {other_patient}"},
        )
        other_listing = platform_client.get(
            f"/questionnaires/appointments/{other_appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {other_patient}"},
        )
        other_assignment_id = other_listing.json()[0]["id"]

        response = platform_client.get(
            f"/questionnaires/assignments/{other_assignment_id}",
            headers={"Authorization": f"Bearer {doctor}"},
        )
        self.assertEqual(response.status_code, 404)


class TestFCancellationPropagation(unittest.TestCase):
    """F. Cancelling an appointment cancels its pending forms."""

    def test_19_cancel_cascades_to_pending_forms(self) -> None:
        fx = QFixture()
        fx.seed_templates()
        appt = fx.book()
        patient = login(fx.patient_email)
        platform_client.post(
            f"/questionnaires/appointments/{appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {patient}"},
        )
        cancel = platform_client.patch(
            f"/appointments/{appt.id}/cancel",
            json={"reason": "schedule conflict"},
            headers={"Authorization": f"Bearer {patient}"},
        )
        self.assertEqual(cancel.status_code, 200, cancel.text)

        listing = platform_client.get(
            f"/questionnaires/appointments/{appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {patient}"},
        )
        for assignment in listing.json():
            self.assertEqual(assignment["status"], "cancelled")
            self.assertEqual(assignment["answers"], {})

    def test_20_completed_form_not_cancelled(self) -> None:
        fx = QFixture()
        fx.seed_templates()
        appt = fx.book(hh=9, mm=0)
        patient = login(fx.patient_email)
        platform_client.post(
            f"/questionnaires/appointments/{appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {patient}"},
        )
        listing = platform_client.get(
            f"/questionnaires/appointments/{appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {patient}"},
        )
        rows = sorted(listing.json(), key=lambda a: a["created_at"] or "")
        standard = [a for a in rows if a["kind"] == "standard"]
        assignment_id = (standard or rows)[0]["id"]
        for key, value in (("reason", "x"), ("fever", "no"), ("severity", 1)):
            platform_client.post(
                f"/questionnaires/assignments/{assignment_id}/answers",
                json={"key": key, "value": value},
                headers={"Authorization": f"Bearer {patient}"},
            )
        platform_client.post(
            f"/questionnaires/assignments/{assignment_id}/complete",
            headers={"Authorization": f"Bearer {patient}"},
        )
        platform_client.patch(
            f"/appointments/{appt.id}/cancel",
            json={"reason": "cancel later"},
            headers={"Authorization": f"Bearer {patient}"},
        )
        response = platform_client.get(
            f"/questionnaires/assignments/{assignment_id}",
            headers={"Authorization": f"Bearer {patient}"},
        )
        self.assertEqual(response.json()["status"], "completed")  # preserved


class TestGAgentCollection(unittest.TestCase):
    """G. Conversational collection through the agent graph.

    Mirrors the Phase 5 convention: the graph's LLM interpreter is patched to
    raise LLMUnavailableError, so these tests exercise the deterministic
    fallback dialogue — no network, no provider quota dependence.
    """

    def setUp(self) -> None:
        self.fx = QFixture()
        self.fx.seed_templates()
        self._llm_patcher = unittest.mock.patch(
            "backend.app.agent.graph.interpret_utterance",
            side_effect=_no_llm,
        )
        self._llm_patcher.start()
        self.addCleanup(self._llm_patcher.stop)

    def _agent_book(self):
        """Search as the agent, then select THIS fixture's doctor's offer — in
        the full suite other fixtures' doctors can lead the directory, so the
        offer cap is raised for this module's searches and the selection is
        explicit rather than 'the first one'."""
        from backend.app.agent import capabilities

        db = PlatformSession()
        try:
            user = db.query(User).filter(User.email == self.fx.patient_email).first()
            conv = ConversationState()
            # Same dialogue the Phase 5 demo used: intent, then visit type.
            state_reply, meta = run_turn(db, user, conv, "any doctor this week")
            if meta["next_action"] == "collect_details":
                state_reply, meta = run_turn(db, user, conv, "in person")
            self.assertEqual(meta["next_action"], "present_availability")
            # The offer list can crowd out this fixture's doctor in the full
            # suite (shared DB, many doctors) — the tests target questionnaire
            # collection, not offer ranking. Present the fixture doctor's REAL
            # engine-verified slot as the offer, exactly as the graph would.
            from backend.app.scheduling import SchedulingService
            today = now_utc().date()
            slots = SchedulingService(db).available_slots(
                self.fx.doctor_id, today, today + timedelta(days=6)
            )
            self.assertTrue(slots, "fixture doctor has no open slots this week")
            mine = {
                "doctor_id": self.fx.doctor_id,
                "doctor_name": f"Dr. A-Q {self.fx.suffix}",
                "specialty": "orthopedics",
                "hospital_id": self.fx.hospital_id,
                "start_at": slots[0].start_at.isoformat(),
                "end_at": slots[0].end_at.isoformat(),
            }
            conv.offered = [mine]
            conv.offered_slot = mine["start_at"]
            reply, meta = run_turn(db, user, conv, mine["start_at"])
            self.assertEqual(meta["next_action"], "questionnaire", reply)
            return db, user, conv, reply, meta
        except Exception:
            db.close()
            raise

    def test_21_booking_reply_includes_first_question(self) -> None:
        db, user, conv, reply, meta = self._agent_book()
        db.close()
        self.assertIn("Before your visit", reply)
        self.assertEqual(meta["questionnaire"]["question_key"], "joint")
        self.assertIn("Which joint?", reply)

    def test_22_collection_advances_turn_by_turn(self) -> None:
        db, user, conv, _, meta = self._agent_book()
        try:
            reply, meta = run_turn(db, user, conv, "shoulder")
            # the only required orthopedics question is answered; the optional
            # injury question may be skipped → form completes and the STANDARD
            # form (also assigned to this booking) chains in immediately.
            self.assertEqual(meta["next_action"], "questionnaire", reply)
            self.assertIn("Main reason", reply)
            # walk the standard form to completion
            reply, meta = run_turn(db, user, conv, "shoulder pain")   # reason
            self.assertIn("fever", reply.lower())
            reply, meta = run_turn(db, user, conv, "no")              # fever
            self.assertIn("1-10", reply)
            reply, meta = run_turn(db, user, conv, "4")               # severity
            self.assertEqual(meta["next_action"], "questionnaire_completed", reply)
            self.assertIn("Thank you", reply)
            self.assertIsNone(conv.questionnaire)  # collection mode exited
        finally:
            db.close()

    def test_23_invalid_answer_reasks(self) -> None:
        db, user, conv, _, _ = self._agent_book()
        try:
            conv.questionnaire["question_key"] = "joint"
            reply, meta = run_turn(db, user, conv, "banana")
            self.assertIn("Sorry", reply)
            self.assertIn("Which joint?", reply)  # re-asked with the options
            self.assertEqual(meta["next_action"], "questionnaire")
        finally:
            db.close()

    def test_24_answers_persisted_in_db(self) -> None:
        db, user, conv, _, _ = self._agent_book()
        try:
            assignment_id = conv.questionnaire["assignment_id"]
            run_turn(db, user, conv, "knee")
            db.expire_all()
            assignment = db.get(QuestionnaireAssignment, assignment_id)
            self.assertEqual(assignment.answers.get("joint"), "Knee")
        finally:
            db.close()

    def test_25_questionnaire_state_survives_persistence_roundtrip(self) -> None:
        conv = ConversationState()
        conv.appointment_id = "appt-x"
        conv.questionnaire = {"assignment_id": "a-1", "question_key": "reason",
                              "question_index": 0, "template_name": "T"}
        restored = ConversationState.from_state(conv.state_dict())
        self.assertEqual(restored.questionnaire["assignment_id"], "a-1")
        self.assertEqual(restored.questionnaire["question_key"], "reason")


class TestHSecurity(unittest.TestCase):
    """H. Auth and RBAC."""

    def test_26_unauthenticated_rejected(self) -> None:
        for path in (
            "/questionnaires/templates",
            "/questionnaires/assignments/whatever",
            "/questionnaires/appointments/x/questionnaires",
        ):
            self.assertEqual(platform_client.get(path).status_code, 401, path)

    def test_27_patient_endpoints_require_patient_role(self) -> None:
        fx = QFixture()
        doctor = login(f"q-dr-{fx.suffix}@test.health")
        response = platform_client.post(
            "/questionnaires/assignments/some-id/answers",
            json={"key": "x", "value": "y"},
            headers={"Authorization": f"Bearer {doctor}"},
        )
        self.assertIn(response.status_code, (401, 403, 404))
        # a doctor (not patient role) must not pass the patient gate:
        # require_patient returns 403.
        response = platform_client.post(
            f"/questionnaires/appointments/{uuid.uuid4().hex}/questionnaires",
            headers={"Authorization": f"Bearer {doctor}"},
        )
        self.assertIn(response.status_code, (403, 404))

    def test_28_patient_sees_own_assignments_only(self) -> None:
        fx = QFixture()
        fx.seed_templates()
        appt = fx.book()
        p1 = login(fx.patient_email)
        p2 = login(fx.patient2_email)
        platform_client.post(
            f"/questionnaires/appointments/{appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {p1}"},
        )
        response = platform_client.get(
            f"/questionnaires/appointments/{appt.id}/questionnaires",
            headers={"Authorization": f"Bearer {p2}"},
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
