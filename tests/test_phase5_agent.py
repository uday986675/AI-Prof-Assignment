"""Phase 5 — AI agent (LangGraph + capability tools) tests.

Run from repo root:  python -m unittest discover -s tests -v

Groups:
  A  slot-filling state machine (unit, deterministic — no LLM)
  B  capabilities: real availability + controlled booking (+ authz)
  C  agent API: sessions, RBAC, ownership isolation
  D  end-to-end conversation (fallback interpreter): clarify → search → book
  E  persistence: state + events survive a "process restart"
  F  graph consumes (stub) LLM updates / falls back on garbage

The LLM layer is deliberately NOT called in tests: interpret_utterance is
monkeypatched to raise LLMUnavailableError, exercising the deterministic
fallback path (which is the production behavior when no API key is set).
Tests that DO provide an update use a stub, never the network.

EHR wiring: the same in-process transport bridge as Phases 3/4 — the
capability layer's EHRSyncService gets a connector that talks to the Mock EHR
ASGI app directly (no live server, no real URLs).
"""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from datetime import date, datetime, time, timedelta

# Throwaway DBs BEFORE any app import (same pattern as Phases 3/4).
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp(prefix='hap-p5-')}/platform.db")
os.environ.setdefault("MOCK_EHR_DATABASE_URL", f"sqlite:///{tempfile.mkdtemp(prefix='hap-p5-')}/ehr.db")
os.environ.setdefault("MOCK_EHR_API_KEY", "test-ehr-key-5")
os.environ.setdefault("APP_ENVIRONMENT", "test")

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.agent import capabilities  # noqa: E402
from backend.app.agent.graph import run_turn  # noqa: E402
from backend.app.agent.llm import LLMUnavailableError  # noqa: E402
from backend.app.agent.service import AgentService  # noqa: E402
from backend.app.agent.state import ConversationState  # noqa: E402
from backend.app.core.security import hash_password  # noqa: E402
from backend.app.database.base import SessionLocal as PlatformSession  # noqa: E402
from backend.app.main import app as platform_app  # noqa: E402
from backend.app.models import (  # noqa: E402
    Appointment,
    ConversationEvent,
    Doctor,
    DoctorAvailability,
    Hospital,
    Patient,
    User,
)
from backend.app.scheduling import SchedulingService, now_utc  # noqa: E402
from backend.app.services import EHRSyncService  # noqa: E402
from tests.test_phase3_ehr import (  # noqa: E402  (proven Phase 3 harness)
    PlatformFixture,
    at,
    future_weekday,
    seed_ehr_provider,
)
from mock_ehr.app.database import Base as EHRBase  # noqa: E402
from mock_ehr.app.database import engine as ehr_engine  # noqa: E402
from mock_ehr.app.models import Appointment as EHRAppointmentRow  # noqa: E402

platform_client = TestClient(platform_app)

PROVIDER = "EHR-PROV-TEST-1"

# The connector a capability's EHRSyncService uses: identical to the Phase 3/4
# harness (in-process transport to the Mock EHR app). make_connector() accepts
# an optional transport so a test can inject faults exactly like Phase 4 does.
def _capability_connector():
    from tests.test_phase3_ehr import make_connector, asgi_transport

    return make_connector(transport=asgi_transport())


def _no_llm(*args, **kwargs):
    raise LLMUnavailableError("tests run without an LLM")


import unittest.mock  # noqa: E402  (used by the shared agent test base)


def login(email: str, password: str = "TestPass1!") -> str:
    resp = platform_client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def auth(email: str) -> dict:
    return {"Authorization": f"Bearer {login(email)}"}


class AgentFixture(PlatformFixture):
    """Extends the Phase 3 fixture with a second (cross-tenant) patient.

    The fixture doctor is renamed "Dr. A-Sync …" so it sorts FIRST in the
    doctor directory: the agent searches the whole approved-hospital network,
    and in the full suite several fixtures ("Dr. API", "Dr. Test") share the
    throwaway DB — the rename makes conversational booking deterministic and
    guarantees the offered doctor always has the seeded EHR provider mapping.
    """

    def __init__(self) -> None:
        super().__init__()
        db = PlatformSession()
        try:
            doctor = db.get(Doctor, self.doctor_id)
            doctor.full_name = f"Dr. A-Sync {uuid.uuid4().hex[:6]}"
            db.commit()
        finally:
            db.close()
        db = PlatformSession()
        try:
            other_user = User(
                email=f"other-{uuid.uuid4().hex[:6]}@test.health",
                password_hash=hash_password("TestPass1!"),
                full_name="Other Patient",
                role="patient",
            )
            db.add(other_user)
            db.flush()
            db.add(Patient(user_id=other_user.id))
            db.commit()
            self.other_patient_email = other_user.email
        finally:
            db.close()


class _AgentTestCase(unittest.TestCase):
    """Shared agent test base: fresh fixture, LLM forced to fallback mode.

    Availability rows are created against a weekday computed at import of the
    Phase 3 harness; if a run crosses midnight the scheduling engine would
    refuse bookings for the now-past date, so tests recompute the date
    (self.fixture.monday) before each test. Tests run under the deterministic
    fallback interpreter — the production behavior when no LLM key is set.
    """

    def setUp(self) -> None:
        EHRBase.metadata.create_all(ehr_engine)
        seed_ehr_provider(PROVIDER)
        self.fixture = AgentFixture()
        self.fixture.monday = future_weekday(0)  # survive midnight crossings
        # Force the deterministic fallback path for every test (no network).
        self._patcher = unittest.mock.patch(
            "backend.app.agent.graph.interpret_utterance", side_effect=_no_llm
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        # The capability layer's EHRSyncService gets the in-process connector
        # (identical to the Phase 3/4 harness) — no live servers, no URLs.
        real_service = capabilities.EHRSyncService

        class _PatchedService(real_service):
            def __init__(self, db, connector=None):
                super().__init__(db, connector=connector or _capability_connector())

        self._ehr_patcher = unittest.mock.patch(
            "backend.app.agent.capabilities.EHRSyncService", new=_PatchedService
        )
        self._ehr_patcher.start()
        self.addCleanup(self._ehr_patcher.stop)


class TestAStateMachine(unittest.TestCase):
    """A — slot-filling state machine, no LLM involved."""

    def test_1_blank_state_asks_visit_type_first(self) -> None:
        state = ConversationState()
        # Preference order: visit type is collected before the specialty.
        self.assertIn("in-person", state.next_question())

    def test_2_specialty_then_type_question(self) -> None:
        state = ConversationState()
        q = state.apply_utterance({"specialty": "orthopedics"})
        self.assertIn("Would you prefer", q)  # next gap: visit type
        self.assertEqual(state.specialty, "orthopedics")

    def test_3_declining_specialty_is_valid(self) -> None:
        state = ConversationState()
        state.apply_utterance({"specialty": ""})
        self.assertEqual(state.specialty, "")
        self.assertIn("in-person", state.next_question())

    def test_4_invalid_when_is_ignored_not_coerced(self) -> None:
        state = ConversationState()
        state.apply_utterance({"when": "next_year"})
        self.assertEqual(state.when, "this_week")  # default kept

    def test_5_invalid_type_is_ignored(self) -> None:
        state = ConversationState()
        state.apply_utterance({"appointment_type": "phone"})
        self.assertIsNone(state.appointment_type)
        # An unrecognised answer simply re-asks the visit-type question.
        self.assertIn("in-person", state.next_question())

    def test_6_slot_selection_skips_questions(self) -> None:
        state = ConversationState(specialty="orthopedics", appointment_type="in_person")
        q = state.apply_utterance({"slot_selected": "2026-10-05T09:00:00"})
        self.assertIsNone(q)
        self.assertEqual(state.offered_slot, "2026-10-05T09:00:00")

    def test_7_state_round_trips_through_json(self) -> None:
        state = ConversationState(specialty="cardiology", when="tomorrow",
                                  appointment_type="video",
                                  offered=[{"doctor_id": "d1", "start_at": "2026-10-05T10:00:00"}])
        restored = ConversationState.from_state(state.state_dict())
        self.assertEqual(restored.specialty, "cardiology")
        self.assertEqual(restored.when, "tomorrow")
        self.assertEqual(restored.appointment_type, "video")
        self.assertEqual(restored.offered, state.offered)
        self.assertIsNone(restored.next_question())  # still ready to search


class TestBCapabilities(_AgentTestCase):
    """B — capability layer: real search + controlled booking."""

    def test_8_search_finds_real_slots(self) -> None:
        db = PlatformSession()
        try:
            user = db.query(User).filter(User.email == self.fixture.patient_user_email).one()
            result = capabilities.search_availability(
                db, user, specialty="orthopedics", when="this_week"
            )
            self.assertGreater(len(result["offers"]), 0)
            offer = result["offers"][0]
            # REAL slots: ISO datetimes produced by the Phase 2 slot engine.
            self.assertIn("T", offer["start_at"])
            self.assertIn("T", offer["end_at"])
            self.assertEqual(offer["specialty"], "orthopedics")
            # Every offer sits inside the requested "this week" window.
            window_end = now_utc().date() + timedelta(days=6)
            self.assertLessEqual(
                datetime.fromisoformat(offer["start_at"]).date(), window_end
            )
        finally:
            db.close()

    def test_9_search_unknown_specialty_reports_options(self) -> None:
        db = PlatformSession()
        try:
            user = db.query(User).filter(User.email == self.fixture.patient_user_email).one()
            result = capabilities.search_availability(
                db, user, specialty="astronomy", when="this_week"
            )
            self.assertEqual(result["offers"], [])
            self.assertIn("orthopedics", result["available_specialties"])
        finally:
            db.close()

    def test_10_booking_creates_appointment_and_syncs_ehr(self) -> None:
        db = PlatformSession()
        try:
            user = db.query(User).filter(User.email == self.fixture.patient_user_email).one()
            result = capabilities.book_appointment(
                db, user,
                doctor_id=self.fixture.doctor_id,
                start_at=at(self.fixture.monday, 10, 30).isoformat(),
                appointment_type="in_person",
                reason="agent test",
            )
            self.assertEqual(result["status"], "booked")
            self.assertEqual(result["ehr_sync_status"], "synced")
            self.assertTrue(result["ehr_appointment_id"].startswith("EHR-"))
            appt = db.get(Appointment, result["appointment_id"])
            self.assertEqual(appt.ehr_sync_status, "synced")
        finally:
            db.close()

    def test_11_slot_conflict_is_reported_not_crashed(self) -> None:
        self.fixture.book(hh=11, mm=30)  # occupy the slot first
        db = PlatformSession()
        try:
            user = db.query(User).filter(User.email == self.fixture.patient_user_email).one()
            with self.assertRaises(capabilities.AgentCapabilityError) as ctx:
                capabilities.book_appointment(
                    db, user,
                    doctor_id=self.fixture.doctor_id,
                    start_at=at(self.fixture.monday, 11, 30).isoformat(),
                )
            self.assertEqual(ctx.exception.reason, "slot_already_booked")
        finally:
            db.close()

    def test_12_non_patient_cannot_book(self) -> None:
        db = PlatformSession()
        try:
            admin = db.query(User).filter(User.email == self.fixture.admin_email).one()
            with self.assertRaises(capabilities.AgentCapabilityError) as ctx:
                capabilities.book_appointment(
                    db, admin,
                    doctor_id=self.fixture.doctor_id,
                    start_at=at(self.fixture.monday, 12, 0).isoformat(),
                )
            self.assertEqual(ctx.exception.reason, "forbidden")
        finally:
            db.close()

    def test_13_foreign_patient_cannot_read_appointment(self) -> None:
        appt = self.fixture.book(hh=11, mm=30)
        db = PlatformSession()
        try:
            other = db.query(User).filter(User.email == self.fixture.other_patient_email).one()
            with self.assertRaises(capabilities.AgentCapabilityError) as ctx:
                capabilities.get_appointment_for_user(db, other, appt.id)
            self.assertEqual(ctx.exception.reason, "appointment_not_found")
        finally:
            db.close()


class TestCApi(_AgentTestCase):
    """C — agent API: sessions, RBAC, ownership isolation."""

    def test_14_patient_creates_session(self) -> None:
        headers = auth(self.fixture.patient_user_email)
        resp = platform_client.post("/agent/sessions", headers=headers)
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["state"]["when"], "this_week")

    def test_15_unauthenticated_is_401(self) -> None:
        self.assertEqual(platform_client.post("/agent/sessions").status_code, 401)
        self.assertEqual(platform_client.get("/agent/sessions").status_code, 401)

    def test_16_non_patient_roles_are_403(self) -> None:
        admin_headers = auth(self.fixture.admin_email)
        self.assertEqual(
            platform_client.post("/agent/sessions", headers=admin_headers).status_code, 403
        )
        # Doctors also cannot start agent sessions (agent is a patient surface).
        db = PlatformSession()
        try:
            doctor_user = User(
                email=f"doc-{uuid.uuid4().hex[:6]}@test.health",
                password_hash=hash_password("TestPass1!"),
                full_name="Doctor X", role="doctor", hospital_id=self.fixture.hospital_id,
            )
            db.add(doctor_user)
            db.commit()
        finally:
            db.close()
        doc_headers = auth(doctor_user.email)
        self.assertEqual(
            platform_client.post("/agent/sessions", headers=doc_headers).status_code, 403
        )

    def test_17_session_read_is_owner_only(self) -> None:
        headers = auth(self.fixture.patient_user_email)
        conv_id = platform_client.post("/agent/sessions", headers=headers).json()["conversation_id"]
        # Owner can read it.
        self.assertEqual(
            platform_client.get(f"/agent/sessions/{conv_id}", headers=headers).status_code, 200
        )
        # A different patient gets 404 (existence hidden).
        other = auth(self.fixture.other_patient_email)
        self.assertEqual(
            platform_client.get(f"/agent/sessions/{conv_id}", headers=other).status_code, 404
        )
        # A hospital admin (even of the involved hospital) gets 404: the
        # conversation belongs to the patient, not to any tenant.
        self.assertEqual(
            platform_client.get(f"/agent/sessions/{conv_id}", headers=auth(self.fixture.admin_email)).status_code,
            404,
        )

    def test_18_message_on_foreign_session_is_404(self) -> None:
        headers = auth(self.fixture.patient_user_email)
        conv_id = platform_client.post("/agent/sessions", headers=headers).json()["conversation_id"]
        other = auth(self.fixture.other_patient_email)
        resp = platform_client.post(
            f"/agent/sessions/{conv_id}/messages", json={"message": "hello"}, headers=other
        )
        self.assertEqual(resp.status_code, 404)

    def test_19_empty_message_is_422(self) -> None:
        headers = auth(self.fixture.patient_user_email)
        conv_id = platform_client.post("/agent/sessions", headers=headers).json()["conversation_id"]
        resp = platform_client.post(
            f"/agent/sessions/{conv_id}/messages", json={"message": "   "}, headers=headers
        )
        self.assertEqual(resp.status_code, 422)


class TestDConversation(_AgentTestCase):
    """D — end-to-end conversation on the fallback path (spec walkthrough)."""

    def _session(self) -> tuple[dict, str]:
        headers = auth(self.fixture.patient_user_email)
        resp = platform_client.post("/agent/sessions", headers=headers)
        return headers, resp.json()["conversation_id"]

    def test_20_clarify_then_search_presents_real_slots(self) -> None:
        headers, conv_id = self._session()
        r1 = platform_client.post(
            f"/agent/sessions/{conv_id}/messages",
            json={"message": "I need an orthopedic doctor"},
            headers=headers,
        )
        self.assertEqual(r1.status_code, 200)
        reply1 = r1.json()["reply"]
        self.assertIn("in-person", reply1)  # clarifying question about visit type
        r2 = platform_client.post(
            f"/agent/sessions/{conv_id}/messages",
            json={"message": "in person"},
            headers=headers,
        )
        reply2 = r2.json()
        self.assertEqual(reply2["meta"]["next_action"], "present_availability")
        # REAL slots from the engine, within the requested "this week" window.
        self.assertIn("Dr. A-Sync", reply2["reply"])  # fixture doctor leads the directory
        self.assertIn("Which one would you like?", reply2["reply"])
        offered = reply2["state"]["offered"]
        self.assertGreater(len(offered), 0)
        self.assertEqual(offered[0]["specialty"], "orthopedics")

    def test_21_number_selects_slot_and_books_with_ehr(self) -> None:
        headers, conv_id = self._session()
        platform_client.post(
            f"/agent/sessions/{conv_id}/messages",
            json={"message": "orthopedics this week, in person"},
            headers=headers,
        )
        r2 = platform_client.post(
            f"/agent/sessions/{conv_id}/messages", json={"message": "1"}, headers=headers
        )
        body = r2.json()
        self.assertEqual(body["meta"]["next_action"], "booked")
        self.assertEqual(body["meta"]["ehr_sync_status"], "synced")
        self.assertIn("Booked!", body["reply"])
        self.assertIn("synchronized", body["reply"])
        # The platform appointment really exists and is EHR-verified.
        db = PlatformSession()
        try:
            appt = db.get(Appointment, body["meta"]["appointment_id"])
            self.assertEqual(appt.ehr_sync_status, "synced")
            self.assertIsNotNone(appt.external_ehr_appointment_id)
        finally:
            db.close()
        # The EHR holds exactly ONE record for this platform appointment.
        from mock_ehr.app.database import SessionLocal as EHRSessionLocal

        session = EHRSessionLocal()
        try:
            ehr_rows = (
                session.query(EHRAppointmentRow)
                .filter(EHRAppointmentRow.source_platform_ref == body["meta"]["appointment_id"])
                .all()
            )
            self.assertEqual(len(ehr_rows), 1)
            self.assertEqual(ehr_rows[0].external_appointment_id, appt.external_ehr_appointment_id)
        finally:
            session.close()

    def test_22_booking_via_agent_never_double_books(self) -> None:
        # Book 09:00 directly (Phase 2 path), then have the agent present slots
        # and try to take the same one — the conflict path must re-present.
        self.fixture.book(hh=9, mm=0)
        headers, conv_id = self._session()
        platform_client.post(
            f"/agent/sessions/{conv_id}/messages",
            json={"message": "orthopedics, in person"},
            headers=headers,
        )
        # The offered list comes from REAL availability: 09:00 must be absent.
        state = platform_client.get(f"/agent/sessions/{conv_id}", headers=headers).json()["state"]
        offered_starts = [o["start_at"][11:16] for o in state["offered"]]
        self.assertNotIn("09:00", offered_starts)

    def test_23_conversation_state_carries_hospital_context(self) -> None:
        headers, conv_id = self._session()
        platform_client.post(
            f"/agent/sessions/{conv_id}/messages",
            json={"message": "orthopedics"},
            headers=headers,
        )
        platform_client.post(
            f"/agent/sessions/{conv_id}/messages", json={"message": "video"}, headers=headers
        )
        detail = platform_client.get(f"/agent/sessions/{conv_id}", headers=headers).json()
        self.assertIsNotNone(detail["hospital_id"])  # learned from the offers


class TestEPersistence(_AgentTestCase):
    """E/F — checkpoint durability and audit trail."""

    def test_24_state_and_events_survive_restart(self) -> None:
        headers = auth(self.fixture.patient_user_email)
        conv_id = platform_client.post("/agent/sessions", headers=headers).json()["conversation_id"]
        platform_client.post(
            f"/agent/sessions/{conv_id}/messages",
            json={"message": "orthopedics, in person"},
            headers=headers,
        )
        # Simulate a process restart: a brand-new service instance over the same DB.
        db = PlatformSession()
        try:
            user = db.query(User).filter(User.email == self.fixture.patient_user_email).one()
            service = AgentService(db)
            detail = service.get_session(conv_id, user)
            state = ConversationState.from_state(detail["state"])
            self.assertEqual(state.specialty, "orthopedics")
            self.assertGreater(len(state.offered), 0)
            events = detail["events"]
            self.assertEqual([e["role"] for e in events], ["user", "agent"])
            self.assertIn("orthopedics", events[0]["content"])
            # The route must serialize the same shape.
            api_detail = platform_client.get(f"/agent/sessions/{conv_id}", headers=headers).json()
            self.assertEqual(len(api_detail["events"]), 2)
            self.assertEqual(api_detail["events"][0]["role"], "user")
        finally:
            db.close()

    def test_25_events_append_in_order_across_turns(self) -> None:
        headers = auth(self.fixture.patient_user_email)
        conv_id = platform_client.post("/agent/sessions", headers=headers).json()["conversation_id"]
        for msg in ("orthopedics", "video", "1"):
            platform_client.post(
                f"/agent/sessions/{conv_id}/messages", json={"message": msg}, headers=headers
            )
        db = PlatformSession()
        try:
            user = db.query(User).filter(User.email == self.fixture.patient_user_email).one()
            detail = AgentService(db).get_session(conv_id, user)
            roles = [e["role"] for e in detail["events"]]
            self.assertEqual(roles, ["user", "agent"] * 3)
        finally:
            db.close()

    def test_26_conversation_marked_booked_after_agent_booking(self) -> None:
        headers = auth(self.fixture.patient_user_email)
        conv_id = platform_client.post("/agent/sessions", headers=headers).json()["conversation_id"]
        platform_client.post(
            f"/agent/sessions/{conv_id}/messages",
            json={"message": "orthopedics, in person"},
            headers=headers,
        )
        body = platform_client.post(
            f"/agent/sessions/{conv_id}/messages", json={"message": "2"}, headers=headers
        ).json()
        self.assertEqual(body["status"], "booked")
        self.assertEqual(body["meta"]["next_action"], "booked")


class TestFGraphWithLLM(_AgentTestCase):
    """F — the graph consumes a (stub) LLM update correctly."""

    def test_27_llm_update_flows_through_graph(self) -> None:
        stub = {
            "specialty": "cardiology",
            "when": "tomorrow",
            "appointment_type": "video",
        }
        with unittest.mock.patch(
            "backend.app.agent.graph.interpret_utterance", return_value=stub
        ):
            db = PlatformSession()
            try:
                user = db.query(User).filter(User.email == self.fixture.patient_user_email).one()
                state = ConversationState()
                reply, meta = run_turn(db, user, state, "heart doctor soon please")
                # Cardiology has NO seeded doctor in this fixture → the agent
                # honestly reports the empty search and surfaces alternatives.
                self.assertEqual(meta["next_action"], "no_availability")
                # …and surfaces the specialties that DO exist in the network.
                self.assertIn("orthopedics", reply)
                self.assertEqual(state.specialty, "cardiology")
                self.assertIsNone(state.next_question())  # ready for a re-search
            finally:
                db.close()

    def test_28_fallback_used_when_llm_returns_garbage(self) -> None:
        with unittest.mock.patch(
            "backend.app.agent.graph.interpret_utterance", return_value={}
        ):
            db = PlatformSession()
            try:
                user = db.query(User).filter(User.email == self.fixture.patient_user_email).one()
                state = ConversationState()
                reply, meta = run_turn(db, user, state, "I need a shoulder doctor this week")
                # The fallback interpreter still extracted the specialty + when;
                # the graph then asks the ONE remaining question (visit type).
                self.assertEqual(state.specialty, "orthopedics")
                self.assertEqual(meta["next_action"], "collect_details")
                self.assertIn("in-person", reply)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
