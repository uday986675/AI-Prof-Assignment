"""Seed demo data.

Run from repo root:  python -m backend.scripts.seed_demo
Creates: platform admin, two hospitals (A approved with doctors + availability, B pending),
hospital admins, a doctor user, and a demo patient.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402

from backend.app.core.security import hash_password  # noqa: E402
from backend.app.database.base import Base, SessionLocal, ensure_sqlite_dir, engine  # noqa: E402
from backend.app import models  # noqa: F402, E402
from backend.app.models import (  # noqa: E402
    BlockedPeriod,
    Doctor,
    DoctorAvailability,
    Hospital,
    Patient,
    QuestionnaireTemplate,
    User,
)

DEFAULTS = {
    "platform_admin@demo.health": "Platform Admin",
    "admin.a@citygeneral.health": "Alice Admin (City General)",
    "admin.b@stmary.health": "Bob Admin (St Mary)",
    "dr.rao@citygeneral.health": "Dr. Rao",
    "dr.mehta@citygeneral.health": "Dr. Mehta",
    "dr.iyer@stmary.health": "Dr. Iyer",
    "patient@demo.health": "Demo Patient",
}
PASSWORD = "Demo1234!"

WEEKDAYS = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}


def upsert_user(db, email: str, full_name: str, role: str, hospital_id: str | None = None) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if user:
        return user
    db.add(
        User(
            email=email,
            password_hash=hash_password(PASSWORD),
            full_name=full_name,
            role=role,
            hospital_id=hospital_id,
        )
    )
    db.flush()
    return db.scalar(select(User).where(User.email == email))


def main() -> None:
    from sqlalchemy import select

    ensure_sqlite_dir()
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # Platform admin
        upsert_user(db, "platform_admin@demo.health", DEFAULTS["platform_admin@demo.health"], "platform_admin")

        # Hospital A (approved, fully configured)
        hosp_a = db.scalar(select(Hospital).where(Hospital.name == "City General Hospital"))
        if hosp_a is None:
            hosp_a = Hospital(
                name="City General Hospital",
                city="Hyderabad",
                address="12 Main Street",
                phone="+91 40 1234 5678",
                departments=["Orthopedics", "Cardiology", "General Medicine"],
                specialties=["orthopedics", "cardiology", "general_medicine"],
                operating_hours={"mon_fri": "08:00-20:00", "sat": "09:00-14:00"},
                status="approved",
            )
            db.add(hosp_a)
            db.flush()

        # Hospital B (submitted, not yet approved)
        hosp_b = db.scalar(select(Hospital).where(Hospital.name == "St Mary Hospital"))
        if hosp_b is None:
            hosp_b = Hospital(
                name="St Mary Hospital",
                city="Vijayawada",
                address="4 Church Road",
                phone="+91 86 6543 2109",
                departments=["Orthopedics", "Neurology"],
                specialties=["orthopedics", "neurology"],
                status="submitted",
            )
            db.add(hosp_b)
            db.flush()

        admin_a = upsert_user(db, "admin.a@citygeneral.health", DEFAULTS["admin.a@citygeneral.health"], "hospital_admin", hosp_a.id)
        admin_b = upsert_user(db, "admin.b@stmary.health", DEFAULTS["admin.b@stmary.health"], "hospital_admin", hosp_b.id)
        dr_rao_user = upsert_user(db, "dr.rao@citygeneral.health", DEFAULTS["dr.rao@citygeneral.health"], "doctor", hosp_a.id)
        dr_mehta_user = upsert_user(db, "dr.mehta@citygeneral.health", DEFAULTS["dr.mehta@citygeneral.health"], "doctor", hosp_a.id)
        dr_iyer_user = upsert_user(db, "dr.iyer@stmary.health", DEFAULTS["dr.iyer@stmary.health"], "doctor", hosp_b.id)

        def ensure_doctor(hospital_id: str, user_id: str, name: str, specialty: str, minutes: int, external_id: str) -> Doctor:
            doctor = db.scalar(select(Doctor).where(Doctor.hospital_id == hospital_id, Doctor.full_name == name))
            if doctor:
                return doctor
            doctor = Doctor(
                hospital_id=hospital_id,
                user_id=user_id,
                full_name=name,
                specialty=specialty,
                qualifications="MBBS, MS (Ortho)" if specialty == "orthopedics" else "MBBS, MD",
                experience_years=12,
                languages=["English", "Telugu"],
                consultation_minutes=minutes,
                appointment_types=["in_person", "video"],
                status="active",
                external_provider_id=external_id,
            )
            db.add(doctor)
            db.flush()
            return doctor

        rao = ensure_doctor(hosp_a.id, dr_rao_user.id, "Dr. Rao", "orthopedics", 30, "EHR-PROV-001")
        mehta = ensure_doctor(hosp_a.id, dr_mehta_user.id, "Dr. Mehta", "cardiology", 30, "EHR-PROV-002")
        iyer = ensure_doctor(hosp_b.id, dr_iyer_user.id, "Dr. Iyer", "orthopedics", 30, "EHR-PROV-003")

        def ensure_availability(doctor: Doctor, weekday: int, start: str, end: str, slot: int = 30) -> None:
            exists = db.scalar(
                select(DoctorAvailability).where(
                    DoctorAvailability.doctor_id == doctor.id,
                    DoctorAvailability.weekday == weekday,
                    DoctorAvailability.start_time == start,
                )
            )
            if exists:
                return
            db.add(
                DoctorAvailability(
                    hospital_id=doctor.hospital_id,
                    doctor_id=doctor.id,
                    weekday=weekday,
                    start_time=start,
                    end_time=end,
                    slot_minutes=slot,
                    appointment_types=["in_person", "video"],
                )
            )

        # Dr. Rao: Mon-Fri 09:00-13:00 and 14:00-17:00
        for wd in range(5):
            ensure_availability(rao, wd, "09:00", "13:00")
            ensure_availability(rao, wd, "14:00", "17:00")
        # Dr. Mehta: Mon/Wed/Fri 10:00-13:00, PLUS Mon 09:00-09:30 so the
        # Phase 5 agent demo always has a near-term bookable slot (additive;
        # existing seeded rows are never modified or removed).
        for wd in (0, 2, 4):
            ensure_availability(mehta, wd, "10:00", "13:00")
        ensure_availability(mehta, 0, "09:00", "09:30")
        # Dr. Iyer: Tue/Thu 09:00-12:00 (hospital B is not approved yet)
        for wd in (1, 3):
            ensure_availability(iyer, wd, "09:00", "12:00")

        # A leave for Dr. Rao (day after tomorrow)
        if not db.scalar(select(BlockedPeriod).where(BlockedPeriod.doctor_id == rao.id)):
            leave_start = (datetime.now() + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
            db.add(
                BlockedPeriod(
                    hospital_id=hosp_a.id,
                    doctor_id=rao.id,
                    kind="leave",
                    reason="Conference",
                    start_at=leave_start,
                    end_at=leave_start + timedelta(days=1),
                )
            )

        # Demo patient
        patient_user = upsert_user(db, "patient@demo.health", DEFAULTS["patient@demo.health"], "patient")
        if not db.scalar(select(Patient).where(Patient.user_id == patient_user.id)):
            db.add(Patient(user_id=patient_user.id, phone="+91 98765 43210", date_of_birth=date(1990, 5, 14)))

        # Phase 6: pre-visit questionnaire templates (additive; idempotent by
        # (hospital_id, name)). Hospital A gets a standard form plus an
        # orthopedics-specific one so bookings with Dr. Rao collect clinically
        # relevant answers in the agent conversation.
        admin_a = db.scalar(select(User).where(User.email == "admin.a@citygeneral.health"))
        STANDARD_QUESTIONS = [
            {"key": "contact_reason", "kind": "free_text", "prompt": "What is the main reason for your visit?", "required": True},
            {"key": "symptoms", "kind": "free_text", "prompt": "Briefly describe your symptoms", "required": True},
            {"key": "symptom_duration", "kind": "single_choice", "prompt": "How long have you had these symptoms?",
             "options": ["Less than a week", "1-4 weeks", "1-6 months", "Over 6 months"], "required": True},
            {"key": "severity", "kind": "scale_1_10", "prompt": "How severe is the pain or discomfort (1 = mild, 10 = severe)?", "required": True},
            {"key": "medications", "kind": "free_text", "prompt": "Are you currently taking any medications? (list or say none)", "required": False},
            {"key": "allergies", "kind": "boolean", "prompt": "Do you have any known drug allergies?", "required": True},
        ]
        ORTHO_QUESTIONS = [
            {"key": "pain_location", "kind": "single_choice", "prompt": "Where is the pain worst?",
             "options": ["Shoulder", "Knee", "Hip", "Back", "Other joint"], "required": True},
            {"key": "injury_history", "kind": "boolean", "prompt": "Did the pain start after an injury or accident?", "required": True},
            {"key": "mobility", "kind": "single_choice", "prompt": "How is your mobility today?",
             "options": ["Normal", "Slightly limited", "Needs support", "Cannot move the joint"], "required": True},
            {"key": "imaging", "kind": "boolean", "prompt": "Have you had an X-ray or MRI for this already?", "required": False},
            {"key": "notes", "kind": "free_text", "prompt": "Anything else the doctor should know before the visit?", "required": False},
        ]
        for name, kind, questions in (
            ("Standard Pre-Visit", "standard", STANDARD_QUESTIONS),
            ("orthopedics", "specialty", ORTHO_QUESTIONS),
        ):
            if not db.scalar(
                select(QuestionnaireTemplate).where(
                    QuestionnaireTemplate.hospital_id == hosp_a.id,
                    QuestionnaireTemplate.name == name,
                )
            ):
                db.add(
                    QuestionnaireTemplate(
                        hospital_id=hosp_a.id,
                        name=name,
                        kind=kind,
                        description=("Routine pre-visit intake" if kind == "standard"
                                     else "Orthopedics-specific pre-visit questions"),
                        questions=questions,
                        created_by_user_id=admin_a.id if admin_a else None,
                    )
                )

        db.commit()
        print("Seed complete.")
        print("Demo accounts (password for all: Demo1234!):")
        for email, name in DEFAULTS.items():
            print(f"  {email:38s} {name}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
