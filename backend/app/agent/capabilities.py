"""Capabilities — the ONLY tools the AI agent may call.

This module is the platform's controlled boundary for the agent (mirrors the
PRD rule: the AI never touches the database or the Mock EHR directly):

    agent graph  →  capabilities  →  SchedulingService / EHRSyncService

Consequently every capability:
  * receives the caller's ``User`` (real authorization context, no spoofing),
  * delegates ALL business rules to the Phase 1–4 services (revalidation,
    double-booking protection, idempotency, correlation, RBAC-aware queries),
  * returns plain dicts (no ORM objects leak into agent prompts).

Search is read-only. Booking is patient-only (same rule as POST /appointments).
EHR synchronization is invoked with the SAME EHRSyncService the REST API uses —
the agent merely triggers it and reports the recorded outcome, never claims an
unverified result.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment, Doctor, Hospital, User
from ..scheduling import SchedulingError, SchedulingService, now_utc
from ..services import EHRSyncError, EHRSyncService, QuestionnaireService

MAX_OFFERS = 5  # offers shown per search turn
WINDOWS = {"today": 0, "tomorrow": 1, "this_week": 6}


class AgentCapabilityError(Exception):
    """A capability refused to act (mirrors SchedulingError semantics)."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


# ----------------------------------------------------------------------
# read-only: real availability search
# ----------------------------------------------------------------------

def _date_window(when: str) -> tuple[date, date]:
    today = now_utc().date()
    offset = WINDOWS.get(when, WINDOWS["this_week"])
    if when == "today":
        return today, today
    if when == "tomorrow":
        return today + timedelta(days=1), today + timedelta(days=1)
    return today, today + timedelta(days=offset)


def find_doctors_by_specialty(db: Session, specialty: str | None) -> list[Doctor]:
    """Active doctors at APPROVED hospitals, optionally filtered by specialty.

    Empty specialty = any doctor (patient declined to name one). Exact match
    first, then substring — never a fuzzy guess across unrelated fields.
    """
    stmt = (
        select(Doctor)
        .join(Hospital, Doctor.hospital_id == Hospital.id)
        .where(Doctor.status == "active", Hospital.status == "approved")
        .order_by(Doctor.full_name)
    )
    doctors = list(db.scalars(stmt))
    if not specialty:
        return doctors
    needle = specialty.strip().lower()
    exact = [d for d in doctors if d.specialty.strip().lower() == needle]
    if exact:
        return exact
    return [d for d in doctors if needle in d.specialty.strip().lower() or d.specialty.strip().lower() in needle]


def search_availability(db: Session, user: User, *, specialty: str | None, when: str,
                        appointment_type: str | None = None) -> dict:
    """Search REAL availability (Phase 2 engine) across approved hospitals."""
    if when not in WINDOWS:
        raise AgentCapabilityError("invalid_when", "when must be today, tomorrow or this_week")

    doctors = find_doctors_by_specialty(db, specialty)
    offers: list[dict] = []
    from_date, to_date = _date_window(when)
    service = SchedulingService(db)
    for doctor in doctors:
        if len(offers) >= MAX_OFFERS:
            break
        try:
            slots = service.available_slots(
                doctor.id, from_date, to_date, appointment_type=appointment_type
            )
        except SchedulingError:
            continue  # doctor not searchable for this caller — skip, never leak
        for slot in slots[: MAX_OFFERS - len(offers)]:
            offers.append(
                {
                    "doctor_id": doctor.id,
                    "doctor_name": doctor.full_name,
                    "specialty": doctor.specialty,
                    "hospital_id": doctor.hospital_id,
                    "start_at": slot.start_at.isoformat(),
                    "end_at": slot.end_at.isoformat(),
                }
            )

    # The menu of what the network offers (ALL specialties with active doctors
    # at approved hospitals) — so an unknown specialty gets a helpful answer,
    # not just a dead end.
    all_specialties = sorted(
        {d.specialty for d in find_doctors_by_specialty(db, None)}
    )
    return {
        "when": when,
        "specialty": specialty or "",
        "offers": offers,
        "available_specialties": all_specialties,
    }


# ----------------------------------------------------------------------
# write: booking through the controlled path
# ----------------------------------------------------------------------

def _patient_user(db: Session, user: User) -> None:
    if user.role != "patient":
        raise AgentCapabilityError("forbidden", "Only patients can book appointments")


def book_appointment(db: Session, user: User, *, doctor_id: str, start_at: str,
                     appointment_type: str | None = None, reason: str | None = None,
                     correlation_id: str | None = None) -> dict:
    """Book a slot, then trigger EHR synchronization via the Phase 3/4 services.

    SchedulingService revalidates the slot inside the booking transaction (and
    the partial unique index backstops the race), so a stale offer cannot
    double-book. The EHR outcome is whatever EHRSyncService recorded —
    synced / failed / unknown / not_attempted — reported verbatim.
    """
    _patient_user(db, user)
    scheduling = SchedulingService(db)
    patient = scheduling.ensure_patient_profile(user)
    try:
        appointment: Appointment = scheduling.book_appointment(
            patient_id=patient.id,
            doctor_id=doctor_id,
            start_at=start_at,
            appointment_type=appointment_type,
            reason=reason,
            actor_user_id=user.id,
            actor_role=user.role,
            correlation_id=correlation_id,
        )
    except SchedulingError as exc:
        raise AgentCapabilityError(exc.reason, exc.message) from exc

    ehr_outcome, ehr_appointment_id, ehr_detail = "not_attempted", None, None
    try:
        appointment = EHRSyncService(db).sync_appointment(appointment.id, actor_user_id=user.id)
        ehr_outcome = appointment.ehr_sync_status or "not_attempted"
        ehr_appointment_id = appointment.external_ehr_appointment_id
    except EHRSyncError as exc:
        # Booking succeeded; EHR push refused BEFORE any write (e.g. the doctor
        # has no external_provider_id). The appointment stands; recovery can
        # retry later — report honestly instead of failing the whole turn.
        ehr_detail = exc.message

    doctor = db.get(Doctor, appointment.doctor_id)
    return {
        "appointment_id": appointment.id,
        "status": appointment.status,
        "doctor_id": appointment.doctor_id,
        "doctor_name": doctor.full_name if doctor else doctor_id,
        "start_at": appointment.start_at.isoformat(),
        "end_at": appointment.end_at.isoformat(),
        "appointment_type": appointment.appointment_type,
        "ehr_sync_status": ehr_outcome,
        "ehr_appointment_id": ehr_appointment_id,
        "ehr_detail": ehr_detail,
    }


# ----------------------------------------------------------------------
# write: Phase 6 conversational questionnaire collection
# ----------------------------------------------------------------------

def start_questionnaire(db: Session, user: User, appointment_id: str) -> dict | None:
    """Ensure questionnaires exist for a freshly booked appointment and return
    the pending one to collect conversationally (None when nothing pending).

    Booking (REST or agent) already auto-assigns; this re-run is idempotent and
    also covers bookings made before Phase 6 existed.
    """
    _patient_user(db, user)
    service = QuestionnaireService(db)
    service.assign_for_appointment(appointment_id, actor_user_id=user.id)
    return next_pending_questionnaire(db, user, appointment_id)


def next_pending_questionnaire(db: Session, user: User, appointment_id: str) -> dict | None:
    """The patient's first pending assignment for an appointment with its next
    unanswered question, ready for the conversational loop."""
    from ..models import Patient, QuestionnaireAssignment

    _patient_user(db, user)
    patient = db.scalar(select(Patient).where(Patient.user_id == user.id))
    if patient is None:
        return None
    assignments = list(
        db.scalars(
            select(QuestionnaireAssignment)
            .where(
                QuestionnaireAssignment.appointment_id == appointment_id,
                QuestionnaireAssignment.patient_id == patient.id,
                QuestionnaireAssignment.status == "pending",
            )
            .order_by(QuestionnaireAssignment.created_at)
        )
    )
    service = QuestionnaireService(db)
    for assignment in assignments:
        question = service.next_question(assignment.id, patient_id=patient.id)
        if question is not None:
            return {
                "assignment_id": assignment.id,
                "template_name": service._assignment_out(assignment)["template_name"],
                "question": question,
            }
    return None


def next_pending_questionnaire_for_assignment(db: Session, user: User, assignment_id: str) -> dict | None:
    """The current open question of ONE assignment (for the collection loop)."""
    from ..models import Patient, QuestionnaireAssignment

    _patient_user(db, user)
    patient = db.scalar(select(Patient).where(Patient.user_id == user.id))
    if patient is None:
        return None
    assignment = db.get(QuestionnaireAssignment, assignment_id)
    if assignment is None or assignment.patient_id != patient.id:
        return None
    service = QuestionnaireService(db)
    question = service.next_question(assignment.id, patient_id=patient.id)
    if question is None:
        return None
    return {"assignment_id": assignment.id, "question": question}


def answer_questionnaire(db: Session, user: User, *, assignment_id: str, key: str,
                         value) -> dict:
    """Record one conversational answer and report collection progress.

    Returns {status, done?, question?}: when a required question is still open
    the caller keeps collecting; when the form is complete the assignment is
    finalized (mandatory-answer gate enforced inside the service).
    """
    from ..models import Patient, QuestionnaireAssignment

    patient = db.scalar(select(Patient).where(Patient.user_id == user.id))
    if patient is None:
        raise AgentCapabilityError("forbidden", "No patient profile")
    service = QuestionnaireService(db)
    try:
        service.submit_answer(assignment_id, patient_id=patient.id, key=key, value=value)
    except Exception as exc:
        # Surface service validation errors as capability errors with a reason.
        message = str(getattr(exc, "detail", "") or exc)
        code = getattr(exc, "status_code", None)
        if code == 404:
            raise AgentCapabilityError("assignment_not_found", "Questionnaire not found") from exc
        if code == 409:
            raise AgentCapabilityError("questionnaire_closed", message or "Questionnaire is closed") from exc
        raise AgentCapabilityError("invalid_answer", message or "Invalid answer") from exc

    assignment_row = db.get(QuestionnaireAssignment, assignment_id)
    assignment = service._assignment_out(assignment_row)
    remaining = service.next_question(assignment_id, patient_id=patient.id)
    questions = assignment["questions"]
    answers = assignment["answers"]
    required_missing = [q["key"] for q in questions if q.get("required") and q["key"] not in answers]
    if not required_missing:
        service.complete(assignment_id, patient_id=patient.id)
        return {"status": "completed", "assignment_id": assignment_id}
    return {
        "status": "collecting",
        "assignment_id": assignment_id,
        "question": remaining,
        "answered_count": len(answers),
        "total_count": len(questions),
    }


def get_appointment_for_user(db: Session, user: User, appointment_id: str) -> dict:
    """Read-back used by the agent's "what did you book?" answers (own scope only)."""
    stmt = select(Appointment).where(Appointment.id == appointment_id)
    appt = db.scalar(stmt)
    if appt is None:
        raise AgentCapabilityError("appointment_not_found", "Appointment not found")
    if user.role == "patient":
        from ..models import Patient

        patient = db.scalar(select(Patient).where(Patient.user_id == user.id))
        if patient is None or appt.patient_id != patient.id:
            raise AgentCapabilityError("appointment_not_found", "Appointment not found")
    elif user.role in ("hospital_admin", "doctor"):
        if appt.hospital_id != user.hospital_id:
            raise AgentCapabilityError("appointment_not_found", "Appointment not found")
    elif user.role != "platform_admin":
        raise AgentCapabilityError("forbidden", "Insufficient role")
    doctor = db.get(Doctor, appt.doctor_id)
    return {
        "appointment_id": appt.id,
        "status": appt.status,
        "doctor_name": doctor.full_name if doctor else None,
        "start_at": appt.start_at.isoformat(),
        "appointment_type": appt.appointment_type,
        "ehr_sync_status": appt.ehr_sync_status,
    }
