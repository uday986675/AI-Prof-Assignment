"""Questionnaire API routes (Phase 6) — thin wrappers over QuestionnaireService.

Endpoints:
    POST   /questionnaires/templates                 hospital admin creates a template
    GET    /questionnaires/templates                 hospital admin lists own templates
    POST   /appointments/{id}/questionnaires         assign forms (idempotent; admin or
                                                     patient-owner; booking calls the service directly)
    GET    /appointments/{id}/questionnaires         patient's forms for one appointment
    POST   /questionnaires/assignments/{id}/answers  one answer (conversational path)
    POST   /questionnaires/assignments/{id}/answers:batch  many answers (form path)
    POST   /questionnaires/assignments/{id}/complete finalize (mandatory answers enforced)
    GET    /questionnaires/assignments/{id}          read one (patient owner / doctor / admin)
    GET    /doctor/questionnaires                    doctor's pending forms for the clinic

Authorization follows the Phase 1 model: patients act only on their own
assignments, hospital admins/doctors only inside their own tenant, platform
admins everywhere. Cross-tenant reads return 404 (existence hidden).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...agent.capabilities import AgentCapabilityError  # noqa: F401  (reused error style)
from ...audit import record_audit
from ...auth import get_current_user, require_hospital_admin, require_patient
from ...database.base import get_db
from ...models import Appointment, Doctor, Patient, QuestionnaireAssignment, QuestionnaireTemplate, User
from ...schemas.questionnaire import (
    AnswerIn,
    AnswersIn,
    AssignmentOut,
    QuestionnaireTemplateCreate,
    QuestionnaireTemplateOut,
)
from ...services import QuestionnaireService

router = APIRouter(prefix="/questionnaires", tags=["questionnaires"])


# ----------------------------------------------------------------------
# templates (hospital admin, tenant-owned)
# ----------------------------------------------------------------------


@router.post("/templates", response_model=QuestionnaireTemplateOut, status_code=status.HTTP_201_CREATED)
def create_template(
    payload: QuestionnaireTemplateCreate,
    user: User = Depends(require_hospital_admin),
    db: Session = Depends(get_db),
) -> QuestionnaireTemplate:
    if user.hospital_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Hospital admin has no hospital")
    questions = QuestionnaireService.validate_questions([q.model_dump() for q in payload.questions])
    existing = db.scalar(
        select(QuestionnaireTemplate).where(
            QuestionnaireTemplate.hospital_id == user.hospital_id,
            QuestionnaireTemplate.name == payload.name,
        )
    )
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A questionnaire template with this name already exists")
    template = QuestionnaireTemplate(
        hospital_id=user.hospital_id,
        name=payload.name.strip(),
        kind=payload.kind,
        description=payload.description,
        questions=questions,
        created_by_user_id=user.id,
    )
    db.add(template)
    record_audit(
        db,
        action="questionnaire.template_created",
        actor_user_id=user.id,
        hospital_id=user.hospital_id,
        resource_type="questionnaire_template",
        resource_id=template.id,
        detail={"name": template.name, "kind": template.kind},
    )
    db.commit()
    db.refresh(template)
    return template


@router.get("/templates", response_model=list[QuestionnaireTemplateOut])
def list_templates(
    user: User = Depends(require_hospital_admin),
    db: Session = Depends(get_db),
) -> list[QuestionnaireTemplate]:
    return list(
        db.scalars(
            select(QuestionnaireTemplate)
            .where(QuestionnaireTemplate.hospital_id == user.hospital_id)
            .order_by(QuestionnaireTemplate.created_at)
        )
    )


# ----------------------------------------------------------------------
# assignment (idempotent) + patient views
# ----------------------------------------------------------------------


def _appointment_for_user(db: Session, user: User, appointment_id: str) -> Appointment:
    """Load an appointment for assignment actions with the platform's scoping:
    patient → own appointment; hospital admin → own tenant; platform admin → any."""
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found")
    if user.role == "platform_admin":
        return appt
    if user.role == "hospital_admin":
        if appt.hospital_id != user.hospital_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found")
        return appt
    if user.role == "patient":
        patient = db.scalar(select(Patient).where(Patient.user_id == user.id))
        if patient is None or appt.patient_id != patient.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found")
        return appt
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")


@router.post("/appointments/{appointment_id}/questionnaires", response_model=list[AssignmentOut],
             status_code=status.HTTP_201_CREATED)
def assign_questionnaires(
    appointment_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[QuestionnaireAssignment]:
    appt = _appointment_for_user(db, user, appointment_id)
    service = QuestionnaireService(db)
    created = service.assign_for_appointment(appt.id, actor_user_id=user.id)
    return created


@router.get("/appointments/{appointment_id}/questionnaires", response_model=list[AssignmentOut])
def list_appointment_questionnaires(
    appointment_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    appt = _appointment_for_user(db, user, appointment_id)
    service = QuestionnaireService(db)
    if user.role == "patient":
        return service.list_for_appointment(appt.id, patient_id=appt.patient_id)
    if user.role == "hospital_admin":
        return service.list_for_appointment(appt.id, hospital_id=appt.hospital_id)
    return service.list_for_appointment(appt.id)


# ----------------------------------------------------------------------
# answers / completion (patient only, own assignment)
# ----------------------------------------------------------------------


def _own_assignment(db: Session, user: User, assignment_id: str) -> QuestionnaireAssignment:
    patient = db.scalar(select(Patient).where(Patient.user_id == user.id))
    if patient is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignment not found")
    assignment = db.get(QuestionnaireAssignment, assignment_id)
    if assignment is None or assignment.patient_id != patient.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignment not found")
    return assignment


@router.post("/assignments/{assignment_id}/answers", response_model=AssignmentOut)
def submit_answer(
    assignment_id: str,
    payload: AnswerIn,
    user: User = Depends(require_patient),
    db: Session = Depends(get_db),
) -> dict:
    service = QuestionnaireService(db)
    return service.submit_answer(assignment_id, patient_id=_patient_id(db, user), key=payload.key, value=payload.value)


@router.post("/assignments/{assignment_id}/answers:batch", response_model=AssignmentOut)
def submit_answers(
    assignment_id: str,
    payload: AnswersIn,
    user: User = Depends(require_patient),
    db: Session = Depends(get_db),
) -> dict:
    service = QuestionnaireService(db)
    return service.submit_answers(assignment_id, patient_id=_patient_id(db, user), answers=payload.answers)


@router.post("/assignments/{assignment_id}/complete", response_model=AssignmentOut)
def complete_assignment(
    assignment_id: str,
    user: User = Depends(require_patient),
    db: Session = Depends(get_db),
) -> dict:
    service = QuestionnaireService(db)
    return service.complete(assignment_id, patient_id=_patient_id(db, user), actor_user_id=user.id)


@router.get("/assignments/{assignment_id}", response_model=AssignmentOut)
def get_assignment(
    assignment_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Patient owner / assignment's doctor / tenant admin / platform admin."""
    assignment = db.get(QuestionnaireAssignment, assignment_id)
    if assignment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignment not found")
    if user.role == "platform_admin":
        pass
    elif user.role == "hospital_admin":
        if assignment.hospital_id != user.hospital_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignment not found")
    elif user.role == "doctor":
        if assignment.doctor_id != _doctor_id(db, user):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignment not found")
    elif user.role == "patient":
        if assignment.patient_id != _patient_id(db, user):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignment not found")
    else:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
    service = QuestionnaireService(db)
    return service._assignment_out(assignment, include_patient=user.role in ("doctor", "hospital_admin", "platform_admin"))


def _patient_id(db: Session, user: User) -> str:
    patient = db.scalar(select(Patient).where(Patient.user_id == user.id))
    if patient is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Patient profile not found")
    return patient.id


def _doctor_id(db: Session, user: User) -> str:
    doctor = db.scalar(select(Doctor).where(Doctor.user_id == user.id))
    if doctor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Doctor profile not found")
    return doctor.id


# ----------------------------------------------------------------------
# doctor view (Phase 6 requirement: doctor sees structured answers)
# ----------------------------------------------------------------------


@router.get("/doctor/mine", response_model=list[AssignmentOut])
def doctor_questionnaires(
    status_filter: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """The signed-in doctor's assignments (optionally by status), newest first."""
    if user.role != "doctor":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Doctor role required")
    doctor_id = _doctor_id(db, user)
    stmt = (
        select(QuestionnaireAssignment)
        .where(QuestionnaireAssignment.doctor_id == doctor_id)
        .order_by(QuestionnaireAssignment.created_at.desc())
    )
    if status_filter:
        if status_filter not in ("pending", "completed", "cancelled"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "status must be pending, completed or cancelled")
        stmt = stmt.where(QuestionnaireAssignment.status == status_filter)
    service = QuestionnaireService(db)
    return [service._assignment_out(a, include_patient=True) for a in db.scalars(stmt).all()]
