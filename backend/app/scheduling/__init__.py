"""Scheduling service — the single source of truth for bookable slots.

Layering (business logic lives HERE, not in API routes and never in AI code):

    API route / (future) AI capability
        -> SchedulingService
            -> database (doctors, availability, blocked periods, appointments)

Slots are never invented: they are generated only from configured
DoctorAvailability windows, minus blocked periods, leave, past times and
existing bookings. Every booking revalidates the slot immediately before the
insert, and a partial unique index makes double booking impossible even when
two requests race.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from ..audit import record_audit
from ..models import (
    ALLOWED_TRANSITIONS,
    Appointment,
    BlockedPeriod,
    Doctor,
    DoctorAvailability,
    Hospital,
    Patient,
    SLOT_BLOCKING_STATUSES,
    User,
)

logger = logging.getLogger("scheduling")

APPOINTMENT_TYPES = ("in_person", "video")
BOOKING_BLOCKING_STATUSES = SLOT_BLOCKING_STATUSES  # ("booked", "pending_external")


def now_utc() -> datetime:
    """Naive UTC 'now' — the platform stores naive UTC datetimes everywhere."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class Slot:
    """One candidate slot. `available=False` whenever `reason` is set."""

    doctor_id: str
    start_at: datetime
    end_at: datetime
    available: bool
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "available": self.available,
            "reason": self.reason,
        }


class SchedulingError(Exception):
    """Scheduling rule violation with a machine-readable reason.

    App-level handlers map these to HTTP: ValidationError -> 422,
    ConflictError -> 409, unknown doctor/appointment -> 404.
    """

    def __init__(self, reason: str, message: str | None = None):
        self.reason = reason
        self.message = message or reason
        super().__init__(self.message)


class ValidationError(SchedulingError):
    """Bad input (times, horizon, appointment type) -> HTTP 422."""


class ConflictError(SchedulingError):
    """Slot unavailable / rule conflict / lost race -> HTTP 409."""


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def parse_datetime_utc(value: str | datetime) -> datetime:
    """Normalize incoming ISO datetimes to naive UTC."""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("invalid_datetime", "datetime must be ISO 8601") from exc
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def iter_days(from_date: date, to_date: date):
    day = from_date
    while day <= to_date:
        yield day
        day += timedelta(days=1)


class SchedulingService:
    """All scheduling rules in one place; stateless apart from the session."""

    HORIZON_DAYS = 60  # bookings/searches limited to the next 60 days

    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get_doctor(self, doctor_id: str, hospital_id: str | None = None) -> Doctor:
        doctor = self.db.get(Doctor, doctor_id)
        if doctor is None or (hospital_id is not None and doctor.hospital_id != hospital_id):
            # Other tenants' doctors are "not found" (existence hidden).
            raise SchedulingError("doctor_not_found", "Doctor not found")
        return doctor

    def get_appointment(
        self,
        appointment_id: str,
        *,
        hospital_id: str | None = None,
        patient_id: str | None = None,
        doctor_id: str | None = None,
    ) -> Appointment:
        appt = self.db.get(Appointment, appointment_id)
        if appt is None:
            raise SchedulingError("appointment_not_found", "Appointment not found")
        scoped = (
            (hospital_id is not None and appt.hospital_id != hospital_id)
            or (patient_id is not None and appt.patient_id != patient_id)
            or (doctor_id is not None and appt.doctor_id != doctor_id)
        )
        if scoped:
            raise SchedulingError("appointment_not_found", "Appointment not found")
        return appt

    def ensure_patient_profile(self, user: User) -> Patient:
        """Return the Patient row for a patient user, creating it on first use."""
        patient = self.db.scalar(select(Patient).where(Patient.user_id == user.id))
        if patient is None:
            patient = Patient(user_id=user.id, phone=user.phone)
            self.db.add(patient)
            self.db.flush()
        return patient

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _hospital_approved(self, hospital_id: str) -> bool:
        hospital = self.db.get(Hospital, hospital_id)
        return bool(hospital and hospital.status == "approved")

    def _active_windows(self, doctor_id: str, weekday: int) -> list[DoctorAvailability]:
        return list(
            self.db.scalars(
                select(DoctorAvailability)
                .where(
                    DoctorAvailability.doctor_id == doctor_id,
                    DoctorAvailability.weekday == weekday,
                    DoctorAvailability.is_active.is_(True),
                )
                .order_by(DoctorAvailability.start_time)
            )
        )

    def _blocked_for_range(self, doctor_id: str, range_start: datetime, range_end: datetime) -> list[BlockedPeriod]:
        return list(
            self.db.scalars(
                select(BlockedPeriod).where(
                    BlockedPeriod.doctor_id == doctor_id,
                    BlockedPeriod.start_at < range_end,
                    BlockedPeriod.end_at > range_start,
                )
            )
        )

    def _booked_appointments(self, doctor_id: str, range_start: datetime, range_end: datetime) -> list[Appointment]:
        return list(
            self.db.scalars(
                select(Appointment).where(
                    Appointment.doctor_id == doctor_id,
                    Appointment.status.in_(BOOKING_BLOCKING_STATUSES),
                    Appointment.start_at < range_end,
                    Appointment.end_at > range_start,
                )
            )
        )

    @staticmethod
    def _type_allowed(doctor: Doctor, window: DoctorAvailability, appointment_type: str | None) -> bool:
        if appointment_type is None:
            return True
        # Window-level restriction wins; falls back to doctor-level; none = unrestricted.
        allowed = window.appointment_types or doctor.appointment_types
        if not allowed:
            return True
        return appointment_type in allowed

    # ------------------------------------------------------------------
    # Availability search
    # ------------------------------------------------------------------

    def search_availability(
        self,
        doctor_id: str,
        from_date: date,
        to_date: date,
        *,
        hospital_id: str | None = None,
        appointment_type: str | None = None,
        include_unavailable: bool = True,
    ) -> dict:
        doctor = self.get_doctor(doctor_id, hospital_id)
        if to_date < from_date:
            raise ValidationError("invalid_range", "to_date must be on or after from_date")
        if (to_date - from_date).days + 1 > self.HORIZON_DAYS:
            raise ValidationError("horizon_exceeded", f"Search window is limited to {self.HORIZON_DAYS} days")

        doctor_active = doctor.status == "active"
        hospital_approved = self._hospital_approved(doctor.hospital_id)
        current = now_utc()

        days_out: list[dict] = []
        for day in iter_days(from_date, to_date):
            weekday = day.weekday()
            day_start = datetime.combine(day, time.min)
            day_end = day_start + timedelta(days=1)

            windows = self._active_windows(doctor.id, weekday) if doctor_active and hospital_approved else []
            day_reason: str | None = None
            if not doctor_active:
                day_reason = "doctor_inactive"
            elif not hospital_approved:
                day_reason = "hospital_not_approved"
            elif not windows:
                day_reason = "no_availability_configured"

            slots: list[Slot] = []
            if windows:
                blocked = self._blocked_for_range(doctor.id, day_start, day_end)
                booked = self._booked_appointments(doctor.id, day_start, day_end)
                for window in windows:
                    win_start = datetime.combine(day, parse_hhmm(window.start_time))
                    win_end = datetime.combine(day, parse_hhmm(window.end_time))
                    step = timedelta(minutes=window.slot_minutes)
                    cursor = win_start
                    while cursor + step <= win_end:
                        slot_end = cursor + step
                        reason: str | None = None
                        if cursor < current:
                            reason = "in_past"
                        elif not self._type_allowed(doctor, window, appointment_type):
                            reason = "appointment_type_not_supported"
                        else:
                            for bp in blocked:
                                if bp.start_at < slot_end and cursor < bp.end_at:
                                    reason = "doctor_on_leave" if bp.kind == "leave" else "slot_blocked"
                                    break
                        if reason is None:
                            for ap in booked:
                                if ap.start_at < slot_end and cursor < ap.end_at:
                                    reason = "slot_already_booked"
                                    break
                        if include_unavailable or reason is None:
                            slots.append(Slot(doctor.id, cursor, slot_end, reason is None, reason))
                        cursor += step

            days_out.append(
                {
                    "date": day.isoformat(),
                    "weekday": weekday,
                    "reason": day_reason,
                    "slots": [s.as_dict() for s in slots],
                }
            )

        return {
            "doctor": {
                "id": doctor.id,
                "full_name": doctor.full_name,
                "specialty": doctor.specialty,
                "status": doctor.status,
            },
            "hospital_approved": hospital_approved,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "days": days_out,
        }

    def available_slots(
        self,
        doctor_id: str,
        from_date: date,
        to_date: date,
        *,
        hospital_id: str | None = None,
        appointment_type: str | None = None,
    ) -> list[Slot]:
        """Flat list of only the currently bookable slots in the range."""
        result = self.search_availability(
            doctor_id,
            from_date,
            to_date,
            hospital_id=hospital_id,
            appointment_type=appointment_type,
            include_unavailable=False,
        )
        slots: list[Slot] = []
        for day in result["days"]:
            for s in day["slots"]:
                if s["available"]:
                    slots.append(
                        Slot(
                            doctor_id=doctor_id,
                            start_at=datetime.fromisoformat(s["start_at"]),
                            end_at=datetime.fromisoformat(s["end_at"]),
                            available=True,
                        )
                    )
        return slots

    # ------------------------------------------------------------------
    # Pre-booking validation (revalidation immediately before insert)
    # ------------------------------------------------------------------

    def validate_slot(
        self,
        doctor_id: str,
        start_at: str | datetime,
        *,
        hospital_id: str | None = None,
        appointment_type: str | None = None,
    ) -> tuple[Doctor, DoctorAvailability, datetime, datetime]:
        """Re-check a slot against every rule right before booking.

        Returns (doctor, matched_window, start, end). Raises
        ValidationError/ConflictError with a precise reason on any violation.
        This runs inside the booking transaction, so a slot another patient
        grabbed moments ago is caught here or by the unique index below it.
        """
        doctor = self.get_doctor(doctor_id, hospital_id)
        start = parse_datetime_utc(start_at)

        if doctor.status != "active":
            raise ConflictError("doctor_inactive", "Doctor is not currently accepting appointments")
        if not self._hospital_approved(doctor.hospital_id):
            raise ConflictError("hospital_not_approved", "Hospital is not approved for scheduling")

        today = now_utc().date()
        if start.date() < today:
            raise ValidationError("in_past", "Cannot book a slot in the past")
        if (start.date() - today).days > self.HORIZON_DAYS:
            raise ValidationError("horizon_exceeded", f"Bookings are limited to {self.HORIZON_DAYS} days ahead")

        day = start.date()
        windows = self._active_windows(doctor.id, day.weekday())
        if not windows:
            raise ConflictError("no_availability_configured", "Doctor has no availability on this date")

        matched: DoctorAvailability | None = None
        for window in windows:
            win_start = datetime.combine(day, parse_hhmm(window.start_time))
            win_end = datetime.combine(day, parse_hhmm(window.end_time))
            step_seconds = window.slot_minutes * 60
            offset = int((start - win_start).total_seconds())
            if (
                win_start <= start
                and start + timedelta(minutes=window.slot_minutes) <= win_end
                and offset >= 0
                and offset % step_seconds == 0
            ):
                matched = window
                break
        if matched is None:
            raise ConflictError("outside_working_hours", "Requested time is not an available slot")

        end = start + timedelta(minutes=matched.slot_minutes)
        if appointment_type is not None and not self._type_allowed(doctor, matched, appointment_type):
            raise ValidationError("appointment_type_not_supported", "Requested appointment type is not offered for this slot")

        day_start = datetime.combine(day, time.min)
        day_end = day_start + timedelta(days=1)
        for bp in self._blocked_for_range(doctor.id, day_start, day_end):
            if bp.start_at < end and start < bp.end_at:
                raise ConflictError(
                    "doctor_on_leave" if bp.kind == "leave" else "slot_blocked",
                    "Doctor is unavailable at this time",
                )
        if self._booked_appointments(doctor.id, start, end):
            raise ConflictError("slot_already_booked", "This slot has just been booked")

        return doctor, matched, start, end

    # ------------------------------------------------------------------
    # Booking
    # ------------------------------------------------------------------

    def book_appointment(
        self,
        *,
        patient_id: str,
        doctor_id: str,
        start_at: str | datetime,
        appointment_type: str | None = None,
        reason: str | None = None,
        actor_user_id: str | None = None,
        actor_role: str | None = None,
        correlation_id: str | None = None,
        hospital_id: str | None = None,
    ) -> Appointment:
        """Create an appointment. Revalidates the slot, then relies on the
        partial unique index so two racing bookings can never both commit."""
        if appointment_type is not None and appointment_type not in APPOINTMENT_TYPES:
            raise ValidationError("invalid_appointment_type", "appointment_type must be in_person or video")

        doctor, window, start, end = self.validate_slot(
            doctor_id, start_at, hospital_id=hospital_id, appointment_type=appointment_type
        )
        appt_type = appointment_type or (window.appointment_types or doctor.appointment_types or ["in_person"])[0]
        if not self._type_allowed(doctor, window, appt_type):
            raise ValidationError("appointment_type_not_supported", "Requested appointment type is not offered for this slot")

        appointment = Appointment(
            hospital_id=doctor.hospital_id,
            patient_id=patient_id,
            doctor_id=doctor.id,
            appointment_type=appt_type,
            start_at=start,
            end_at=end,
            status="booked",
            reason=reason,
            correlation_id=correlation_id,
            created_by_user_id=actor_user_id,
        )
        self.db.add(appointment)
        try:
            self.db.flush()
        except IntegrityError as exc:
            # Lost the race: the partial unique index rejected the second insert.
            self.db.rollback()
            raise ConflictError(
                "slot_already_booked", "This slot was booked by someone else moments ago"
            ) from exc
        except OperationalError as exc:
            # SQLite can surface contention as "database is locked" — the slot is
            # still protected; the caller should retry with a fresh availability
            # search instead of blindly re-posting.
            self.db.rollback()
            text = str(exc).lower()
            if "locked" in text or "busy" in text:
                raise ConflictError(
                    "slot_contention",
                    "Another booking for this slot is being processed; please retry",
                ) from exc
            raise
        record_audit(
            self.db,
            action="appointment.created",
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            hospital_id=doctor.hospital_id,
            resource_type="appointment",
            resource_id=appointment.id,
            correlation_id=correlation_id,
            detail={
                "doctor_id": doctor.id,
                "start_at": start.isoformat(),
                "appointment_type": appt_type,
            },
        )
        self.db.commit()
        self.db.refresh(appointment)
        logger.info(
            "appointment.created id=%s doctor=%s start=%s correlation=%s",
            appointment.id, doctor.id, start.isoformat(), correlation_id,
        )
        return appointment

    # ------------------------------------------------------------------
    # Appointment queries and state transitions
    # ------------------------------------------------------------------

    def list_appointments(
        self,
        *,
        doctor_id: str | None = None,
        patient_id: str | None = None,
        hospital_id: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[Appointment]:
        query = select(Appointment).order_by(Appointment.start_at)
        if doctor_id is not None:
            query = query.where(Appointment.doctor_id == doctor_id)
        if patient_id is not None:
            query = query.where(Appointment.patient_id == patient_id)
        if hospital_id is not None:
            query = query.where(Appointment.hospital_id == hospital_id)
        if from_date is not None:
            query = query.where(Appointment.start_at >= datetime.combine(from_date, time.min))
        if to_date is not None:
            query = query.where(Appointment.start_at < datetime.combine(to_date, time.min) + timedelta(days=1))
        return list(self.db.scalars(query))

    def cancel_appointment(
        self,
        appointment_id: str,
        *,
        cancelled_by_user_id: str | None = None,
        reason: str | None = None,
        hospital_id: str | None = None,
        patient_id: str | None = None,
        doctor_id: str | None = None,
    ) -> Appointment:
        appointment = self.get_appointment(
            appointment_id, hospital_id=hospital_id, patient_id=patient_id, doctor_id=doctor_id
        )
        if "cancelled" not in ALLOWED_TRANSITIONS.get(appointment.status, set()):
            raise ConflictError(
                "invalid_transition", f"Cannot cancel an appointment in status '{appointment.status}'"
            )
        if appointment.ehr_sync_status == "unknown":
            # Phase 4 state-safety: with the EHR outcome unresolved, cancelling
            # here would strand a live EHR appointment (unknown → cancelled is
            # a forbidden EHR-sync transition). Reconcile first — once the EHR
            # state is known (synced/failed), cancellation can propagate there.
            raise ConflictError(
                "ehr_outcome_unknown",
                "Cannot cancel while the EHR synchronization outcome is unknown; "
                "reconcile the appointment first (POST /appointments/{id}/reconcile-ehr)",
            )
        appointment.status = "cancelled"
        appointment.cancelled_by_user_id = cancelled_by_user_id
        appointment.cancel_reason = reason
        # Phase 6: cancel this appointment's pending pre-visit questionnaires
        # in the SAME transaction (no stranded pending forms). Lazy import keeps
        # scheduling independent of the questionnaire module.
        from ..services import QuestionnaireService

        QuestionnaireService(self.db).handle_cancellation(appointment.id)
        record_audit(
            self.db,
            action="appointment.cancelled",
            actor_user_id=cancelled_by_user_id,
            hospital_id=appointment.hospital_id,
            resource_type="appointment",
            resource_id=appointment.id,
            detail={"previous_status": "booked"},
        )
        self.db.commit()
        self.db.refresh(appointment)
        logger.info("appointment.cancelled id=%s", appointment.id)
        return appointment
