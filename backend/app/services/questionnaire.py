"""QuestionnaireService — assignment, validation, and submission (Phase 6).

All questionnaire business rules live here; the API routes (thin) and the AI
agent (via a capability) both go through this service:

    assign_for_appointment    auto-assign the right template(s) post-booking
    list_for_appointment      patient view of their forms
    submit_answer             validate + store ONE answer (conversational path)
    submit_answers            validate + store MANY answers (form path)
    complete                  require mandatory answers → mark completed
    get_for_doctor            doctor's pre-visit view (structured answers)

Design decisions that mirror the platform's existing conventions:
  * tenant isolation everywhere — every query is scoped to the appointment's
    hospital / patient / doctor, never just by row id;
  * errors are domain errors (HTTPException) with precise codes, mapped to
    404 / 422 / 409 by the routes;
  * answers are keyed by question ``key`` and validated against the template
    BEFORE storage — free text is trimmed, choices must be listed options.
"""
from __future__ import annotations

import re
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import record_audit
from ..models import (
    QUESTION_KINDS,
    Appointment,
    Doctor,
    Patient,
    QuestionnaireAssignment,
    QuestionnaireTemplate,
    User,
)
from ..models.user import utcnow


class QuestionnaireService:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # template validation
    # ------------------------------------------------------------------

    @staticmethod
    def validate_questions(questions: list[dict]) -> list[dict]:
        """Validate a template's question list (used on template creation)."""
        if not isinstance(questions, list) or not questions:
            raise HTTPException(422, "questions must be a non-empty list")
        seen: set[str] = set()
        for q in questions:
            if not isinstance(q, dict):
                raise HTTPException(422, "each question must be an object")
            key = (q.get("key") or "").strip()
            kind = q.get("kind")
            prompt = (q.get("prompt") or "").strip()
            if not key or not prompt:
                raise HTTPException(422, "each question needs a key and a prompt")
            if key in seen:
                raise HTTPException(422, f"duplicate question key: {key}")
            seen.add(key)
            if kind not in QUESTION_KINDS:
                raise HTTPException(422, f"question {key}: kind must be one of {QUESTION_KINDS}")
            if kind in ("single_choice", "multiple_choice"):
                options = q.get("options")
                if not isinstance(options, list) or not options or not all(isinstance(o, str) and o.strip() for o in options):
                    raise HTTPException(422, f"question {key}: choice questions need non-empty options")
        return questions

    # ------------------------------------------------------------------
    # assignment
    # ------------------------------------------------------------------

    def _templates_for(self, appointment: Appointment) -> list[QuestionnaireTemplate]:
        """Pick the template(s) for an appointment: specialty-specific first,
        then the hospital's standard form. Only ACTIVE templates apply."""
        templates = list(
            self.db.scalars(
                select(QuestionnaireTemplate)
                .where(
                    QuestionnaireTemplate.hospital_id == appointment.hospital_id,
                    QuestionnaireTemplate.is_active.is_(True),
                )
                .order_by(QuestionnaireTemplate.created_at)
            )
        )
        doctor = self.db.get(Doctor, appointment.doctor_id)
        specialty = (doctor.specialty if doctor else "").strip().lower()
        chosen: list[QuestionnaireTemplate] = []
        for t in templates:
            if t.kind == "specialty" and specialty and (
                # The specialty keyword must appear as a whole word in the
                # template name ("orthopedics intake", "Orthopedics v2"),
                # not as a substring of an unrelated word.
                re.search(rf"\b{re.escape(specialty)}\b", t.name.lower()) is not None
            ):
                chosen.append(t)
        standard = [t for t in templates if t.kind == "standard"]
        chosen.extend(standard[:1])
        return chosen

    def assign_for_appointment(
        self,
        appointment_id: str,
        *,
        actor_user_id: str | None = None,
        conversation_id: str | None = None,
    ) -> list[QuestionnaireAssignment]:
        """Auto-assign the right form(s) for a booked appointment.

        Idempotent: (appointment_id, template_id) is unique, so calling this
        twice (booking path + agent path) never duplicates assignments.
        """
        appointment = self.db.get(Appointment, appointment_id)
        if appointment is None:
            raise HTTPException(404, "Appointment not found")

        created: list[QuestionnaireAssignment] = []
        for template in self._templates_for(appointment):
            existing = self.db.scalar(
                select(QuestionnaireAssignment).where(
                    QuestionnaireAssignment.appointment_id == appointment_id,
                    QuestionnaireAssignment.template_id == template.id,
                )
            )
            if existing is not None:
                created.append(existing)  # idempotent re-run
                continue
            assignment = QuestionnaireAssignment(
                hospital_id=appointment.hospital_id,
                appointment_id=appointment_id,
                template_id=template.id,
                patient_id=appointment.patient_id,
                doctor_id=appointment.doctor_id,
                status="pending",
                conversation_id=conversation_id,
                assigned_by_user_id=actor_user_id,
            )
            self.db.add(assignment)
            record_audit(
                self.db,
                action="questionnaire.assigned",
                actor_user_id=actor_user_id,
                hospital_id=appointment.hospital_id,
                resource_type="questionnaire_assignment",
                resource_id=appointment_id,
                detail={"template_id": template.id, "template_name": template.name},
            )
            created.append(assignment)
        self.db.commit()
        for a in created:
            self.db.refresh(a)
        return created

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def list_for_appointment(self, appointment_id: str, *, patient_id: str | None = None,
                             hospital_id: str | None = None) -> list[dict]:
        """Patient/admin view of the forms for one appointment (scoped)."""
        appointment = self.db.get(Appointment, appointment_id)
        if appointment is None:
            raise HTTPException(404, "Appointment not found")
        if patient_id is not None and appointment.patient_id != patient_id:
            raise HTTPException(404, "Appointment not found")
        if hospital_id is not None and appointment.hospital_id != hospital_id:
            raise HTTPException(404, "Appointment not found")
        assignments = list(
            self.db.scalars(
                select(QuestionnaireAssignment)
                .where(QuestionnaireAssignment.appointment_id == appointment_id)
                .order_by(QuestionnaireAssignment.created_at)
            )
        )
        return [self._assignment_out(a) for a in assignments]

    def get_for_doctor(self, assignment_id: str, *, hospital_id: str | None = None,
                       doctor_id: str | None = None) -> dict:
        """Doctor's pre-visit view: template + structured answers + patient context."""
        assignment = self.db.get(QuestionnaireAssignment, assignment_id)
        if assignment is None:
            raise HTTPException(404, "Assignment not found")
        if hospital_id is not None and assignment.hospital_id != hospital_id:
            raise HTTPException(404, "Assignment not found")
        if doctor_id is not None and assignment.doctor_id != doctor_id:
            raise HTTPException(404, "Assignment not found")
        return self._assignment_out(assignment, include_patient=True)

    def _assignment_out(self, a: QuestionnaireAssignment, *, include_patient: bool = False) -> dict:
        template = self.db.get(QuestionnaireTemplate, a.template_id)
        out = {
            "id": a.id,
            "appointment_id": a.appointment_id,
            "template_id": a.template_id,
            "template_name": template.name if template else None,
            "kind": template.kind if template else None,
            "questions": template.questions if template else [],
            "status": a.status,
            "answers": a.answers or {},
            "conversation_id": a.conversation_id,
            "created_at": a.created_at,
            "completed_at": a.completed_at,
        }
        if include_patient:
            patient = self.db.get(Patient, a.patient_id)
            user_id = patient.user_id if patient else None
            user = self.db.get(User, user_id) if user_id else None
            out["patient"] = {
                "patient_id": a.patient_id,
                "full_name": user.full_name if user else None,
            }
            appointment = self.db.get(Appointment, a.appointment_id)
            if appointment is not None:
                out["appointment"] = {
                    "start_at": appointment.start_at,
                    "appointment_type": appointment.appointment_type,
                    "status": appointment.status,
                }
        return out

    # ------------------------------------------------------------------
    # answers
    # ------------------------------------------------------------------

    def _template_questions(self, template_id: str) -> list[dict]:
        template = self.db.get(QuestionnaireTemplate, template_id)
        if template is None:
            raise HTTPException(404, "Template not found")
        return template.questions or []

    @staticmethod
    def validate_answer(question: dict, value) -> object:
        """Validate one answer value against its question definition.

        Returns the normalized value; raises HTTPException(422) on violation.
        """
        key = question.get("key")
        kind = question.get("kind")
        required = bool(question.get("required"))

        if value is None or (isinstance(value, str) and value.strip() == ""):
            if required:
                raise HTTPException(422, f"Answer to '{key}' is required")
            return None

        if kind == "free_text":
            text = str(value).strip()
            if len(text) > 2000:
                raise HTTPException(422, f"Answer to '{key}' is too long (max 2000 chars)")
            return text
        if kind == "boolean":
            if isinstance(value, bool):
                return value
            lowered = str(value).strip().lower()
            if lowered in ("yes", "true", "y", "1"):
                return True
            if lowered in ("no", "false", "n", "0"):
                return False
            raise HTTPException(422, f"Answer to '{key}' must be yes or no")
        if kind == "single_choice":
            options = question.get("options") or []
            text = str(value).strip()
            if text not in options:
                raise HTTPException(422, f"Answer to '{key}' must be one of: {', '.join(options)}")
            return text
        if kind == "multiple_choice":
            options = question.get("options") or []
            values = value if isinstance(value, list) else [v.strip() for v in str(value).split(",")]
            cleaned: list[str] = []
            for item in values:
                item = str(item).strip()
                if not item:
                    continue
                if item not in options:
                    raise HTTPException(422, f"Answer to '{key}' must be chosen from: {', '.join(options)}")
                if item not in cleaned:
                    cleaned.append(item)
            return cleaned
        if kind == "scale_1_10":
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(422, f"Answer to '{key}' must be an integer 1–10") from exc
            if not 1 <= number <= 10:
                raise HTTPException(422, f"Answer to '{key}' must be an integer 1–10")
            return number
        raise HTTPException(422, f"Unknown question kind: {kind}")

    def _load_assignment_for_patient(self, assignment_id: str, patient_id: str) -> QuestionnaireAssignment:
        assignment = self.db.get(QuestionnaireAssignment, assignment_id)
        if assignment is None or assignment.patient_id != patient_id:
            # Existence hidden across patients (platform 404 convention).
            raise HTTPException(404, "Assignment not found")
        if assignment.status == "cancelled":
            raise HTTPException(409, "This questionnaire was cancelled")
        if assignment.status == "completed":
            raise HTTPException(409, "This questionnaire is already completed")
        return assignment

    def submit_answer(self, assignment_id: str, *, patient_id: str, key: str, value) -> dict:
        """Validate + store ONE answer (the conversational collection path)."""
        assignment = self._load_assignment_for_patient(assignment_id, patient_id)
        questions = self._template_questions(assignment.template_id)
        question = next((q for q in questions if q.get("key") == key), None)
        if question is None:
            raise HTTPException(422, f"Unknown question: {key}")
        answer = self.validate_answer(question, value)

        answers = dict(assignment.answers or {})
        if answer is None:
            answers.pop(key, None)
        else:
            answers[key] = answer
        assignment.answers = answers
        record_audit(
            self.db,
            action="questionnaire.answer_recorded",
            actor_user_id=assignment.patient_id,
            hospital_id=assignment.hospital_id,
            resource_type="questionnaire_assignment",
            resource_id=assignment.id,
            detail={"question_key": key},
        )
        self.db.commit()
        self.db.refresh(assignment)
        return self._assignment_out(assignment)

    def submit_answers(self, assignment_id: str, *, patient_id: str, answers: dict) -> dict:
        """Validate + store many answers at once (the form-submission path)."""
        assignment = self._load_assignment_for_patient(assignment_id, patient_id)
        questions = self._template_questions(assignment.template_id)
        by_key = {q.get("key"): q for q in questions}
        unknown = [k for k in answers if k not in by_key]
        if unknown:
            raise HTTPException(422, f"Unknown question(s): {', '.join(sorted(unknown))}")
        merged = dict(assignment.answers or {})
        for key, value in answers.items():
            answer = self.validate_answer(by_key[key], value)
            if answer is None:
                merged.pop(key, None)
            else:
                merged[key] = answer
        assignment.answers = merged
        self.db.commit()
        self.db.refresh(assignment)
        return self._assignment_out(assignment)

    def complete(self, assignment_id: str, *, patient_id: str,
                 actor_user_id: str | None = None) -> dict:
        """Mark completed — allowed only when every required question has an answer."""
        assignment = self._load_assignment_for_patient(assignment_id, patient_id)
        questions = self._template_questions(assignment.template_id)
        answers = assignment.answers or {}
        missing = [q["key"] for q in questions if q.get("required") and q["key"] not in answers]
        if missing:
            raise HTTPException(422, f"Missing required answer(s): {', '.join(missing)}")
        assignment.status = "completed"
        assignment.completed_at = utcnow()
        record_audit(
            self.db,
            action="questionnaire.completed",
            actor_user_id=actor_user_id or assignment.patient_id,
            hospital_id=assignment.hospital_id,
            resource_type="questionnaire_assignment",
            resource_id=assignment.id,
        )
        self.db.commit()
        self.db.refresh(assignment)
        return self._assignment_out(assignment)

    def next_question(self, assignment_id: str, *, patient_id: str) -> dict | None:
        """The first question (in template order) without an answer — for the
        conversational collector. None when everything required is answered."""
        assignment = self.db.get(QuestionnaireAssignment, assignment_id)
        if assignment is None or assignment.patient_id != patient_id:
            raise HTTPException(404, "Assignment not found")
        questions = self._template_questions(assignment.template_id)
        answers = assignment.answers or {}
        for q in questions:
            if q["key"] not in answers:
                return q
        return None

    # ------------------------------------------------------------------
    # lifecycle hooks (called from the scheduling cancel path)
    # ------------------------------------------------------------------

    def handle_cancellation(self, appointment_id: str) -> int:
        """Cancel pending assignments when their appointment is cancelled.

        Returns the number of assignments cancelled. Called inside the
        scheduling transaction BEFORE commit.
        """
        assignments = list(
            self.db.scalars(
                select(QuestionnaireAssignment).where(
                    QuestionnaireAssignment.appointment_id == appointment_id,
                    QuestionnaireAssignment.status == "pending",
                )
            )
        )
        for assignment in assignments:
            assignment.status = "cancelled"
            record_audit(
                self.db,
                action="questionnaire.cancelled",
                hospital_id=assignment.hospital_id,
                resource_type="questionnaire_assignment",
                resource_id=assignment.id,
                detail={"appointment_id": appointment_id},
            )
        return len(assignments)
