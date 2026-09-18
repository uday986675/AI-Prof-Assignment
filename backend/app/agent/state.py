"""Slot-filling dialogue state — the agent's brain outside the LLM.

The LLM classifies/extracts; THIS module owns the conversation semantics:
mandatory-vs-optional fields, validation gates, and which question to ask next.
Kept as a plain dataclass (no LLM involvement) so the whole flow is testable
deterministically — the LLM is an input translator, never the source of truth.

Conversation fields (mirroring AppointmentCreate + SchedulingService rules):

  specialty    — free text until matched against hospital specialties (optional:
                 "" means "any family medicine doctor" is acceptable)
  when         — "this_week" | "today" | "tomorrow" (default this_week)
  appointment_type — in_person | video | None (optional)
  hospital_id  — set when a doctor's availability is viewed    offered / offered_slot — the availability presented to the patient
  appointment_id / ehr_outcome — set after the booking capability runs
  questionnaire — Phase 6: set when the booking path auto-assigns pre-visit
                 questionnaires. Holds the assignment id / template / the
                 pending question (with its index), driving the conversational
                 collection turn-by-turn until the form is completed.

``apply_utterance`` folds a LLM-extracted update (or a slot selection) into the
state and returns which slot-filling question to ask next (None = ready).
``state_dict`` / ``from_state`` make the state JSON-persistable, so a
conversation resumed in a new process behaves identically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import APPOINTMENT_TYPES

# Question the agent asks for each missing mandatory/optional field.
QUESTIONS: dict[str, str] = {
    "specialty": "What kind of doctor would you like to see? (e.g. orthopedics, cardiology)",
    "when": "When would you like the appointment? (today, tomorrow, or this week)",
    "appointment_type": "Would you prefer an in-person visit or a video consultation?",
}


@dataclass
class ConversationState:
    specialty: str | None = None
    when: str = "this_week"
    appointment_type: str | None = None
    hospital_id: str | None = None
    offered: list[dict] = field(default_factory=list)
    offered_slot: str | None = None
    appointment_id: str | None = None
    ehr_outcome: str | None = None
    questionnaire: dict | None = None  # Phase 6 conversational collection state

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def state_dict(self) -> dict[str, Any]:
        return {
            "specialty": self.specialty,
            "when": self.when,
            "appointment_type": self.appointment_type,
            "hospital_id": self.hospital_id,
            "offered": self.offered,
            "offered_slot": self.offered_slot,
            "appointment_id": self.appointment_id,
            "ehr_outcome": self.ehr_outcome,
            "questionnaire": self.questionnaire,
        }

    @classmethod
    def from_state(cls, data: dict[str, Any] | None) -> "ConversationState":
        data = data or {}
        return cls(
            specialty=data.get("specialty"),
            when=data.get("when") or "this_week",
            appointment_type=data.get("appointment_type"),
            hospital_id=data.get("hospital_id"),
            offered=list(data.get("offered") or []),
            offered_slot=data.get("offered_slot"),
            appointment_id=data.get("appointment_id"),
            ehr_outcome=data.get("ehr_outcome"),
            questionnaire=dict(data["questionnaire"]) if data.get("questionnaire") else None,
        )

    # ------------------------------------------------------------------
    # slot-filling semantics
    # ------------------------------------------------------------------

    def missing_mandatory(self) -> list[str]:
        """Fields that must be collected before a search makes sense.

        Specialty is deliberately optional: "book me a family medicine doctor
        this week" is actionable without naming one, so an empty specialty is
        only a question when the patient gave SOMETHING that did not validate.
        """
        missing: list[str] = []
        if self.specialty is None:
            missing.append("specialty")
        return missing

    def next_question(self) -> str | None:
        """The single best question to ask next (None = ready to act).

        Preference order: collect the visit type BEFORE the specialty — the
        natural dialogue is "what do you need / how do you want to be seen",
        and both questions are needed before a search makes sense.
        """
        if self.appointment_type not in APPOINTMENT_TYPES:
            return QUESTIONS["appointment_type"]
        missing = self.missing_mandatory()
        if missing:
            return QUESTIONS[missing[0]]
        return None

    # ------------------------------------------------------------------
    # LLM-update folding
    # ------------------------------------------------------------------

    def apply_utterance(self, update: dict[str, Any]) -> str | None:
        """Fold an LLM-extracted update (or a slot selection) into the state.

        Returns the next question to ask (None = the agent can act). Invalid
        values are dropped with a question, never guessed or coerced.
        """
        if update.get("slot_selected"):
            # Booking a previously offered slot — everything else is already set.
            self.offered_slot = str(update["slot_selected"])
            return None

        if "specialty" in update:
            value = update["specialty"]
            if value is None or (isinstance(value, str) and value.strip() == ""):
                # Explicitly declining to name a specialty is a valid answer.
                self.specialty = ""
            else:
                self.specialty = str(value).strip().lower()

        if "when" in update:
            value = str(update["when"] or "").strip().lower()
            if value in ("today", "tomorrow", "this_week"):
                self.when = value

        if "appointment_type" in update:
            value = str(update["appointment_type"] or "").strip().lower()
            if value in APPOINTMENT_TYPES:
                self.appointment_type = value
            elif value in ("", "no", "none", "either"):
                self.appointment_type = None
            # invalid types are ignored → the question is simply re-asked

        return self.next_question()
