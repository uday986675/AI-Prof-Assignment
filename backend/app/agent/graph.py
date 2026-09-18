"""LangGraph conversation graph — intent classification + slot-filling + booking.

Deterministic-first design: the graph is a real LangGraph StateGraph whose
conditional edges route each turn through

    classify ──► collect_details            (need more info → ask, END)
             ──► present_availability       (ready → search REAL slots, END)
             ──► book                       (slot picked → controlled booking, END)

The LLM (llm.py) is an input translator inside ``classify`` ONLY. When no LLM
is configured, unreachable, or unhelpful, the deterministic fallback interpreter
takes over — the conversation ALWAYS advances to a question, an availability
presentation, or a booking; it can never stall or invent data.

Cross-turn memory (ConversationState) is persisted by AgentService in the
platform database, NOT in the graph: the graph is a per-turn orchestrator and
stays stateless, which is what makes restart-safe conversations possible.
"""
from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from ..models import User
from . import capabilities
from .llm import LLMUnavailableError, interpret_utterance
from .state import ConversationState

logger = logging.getLogger("agent.graph")

# Error reason → patient-appropriate explanation (no internal jargon).
REASON_MESSAGES = {
    "slot_already_booked": "That slot was just taken. Here's what's still open:",
    "doctor_inactive": "That doctor isn't accepting appointments right now. Here's what's available:",
    "hospital_not_approved": "That hospital isn't open for scheduling yet. Here's what's available:",
    "slot_blocked": "That time is no longer offered. Here's what's available:",
    "doctor_on_leave": "The doctor is on leave then. Here's what's available:",
    "outside_working_hours": "That time isn't an available slot. Here's what's open:",
    "doctor_not_found": "I couldn't find that doctor. Let's start the search again:",
    "forbidden": "I'm not able to book for this account.",
    "appointment_not_found": "I couldn't find that appointment.",
    "invalid_when": "I can only search today, tomorrow or this week.",
    "invalid_appointment_type": "Visits are in-person or video only.",
}


class GraphState(TypedDict, total=False):
    """Per-turn graph state. ``db``/``user``/``conv`` are runtime handles passed
    through the graph within a single turn invocation (never checkpointed)."""

    db: Session
    user: User
    conv: ConversationState
    message: str
    correlation_id: str | None
    update: dict[str, Any]
    question: str | None
    reply: str
    meta: dict[str, Any]


# ----------------------------------------------------------------------
# deterministic fallback interpreter (LLM unavailable / useless)
# ----------------------------------------------------------------------

_FALLBACK_SPECIALTIES = (
    "orthopedics", "orthopedic", "ortho", "bone", "shoulder",
    "cardiology", "cardio", "heart",
    "dermatology", "neurology", "pediatrics", "psychiatry",
    "ophthalmology", "gynecology", "gynaecology", "ent",
    "general medicine", "general_medicine",
)


def _fallback_interpret(conv: ConversationState, message: str) -> dict[str, Any]:
    """Keyword mapping over the SAME controlled vocabulary the LLM is asked for.

    Conservative on purpose: it can only ASK or SEARCH — it never invents a
    specialty, a slot, or a booking.
    """
    text = message.strip().lower()
    update: dict[str, Any] = {}

    if any(w in text for w in ("in person", "in-person", "clinic", "hospital visit")):
        update["appointment_type"] = "in_person"
    elif any(w in text for w in ("video", "online", "call")):
        update["appointment_type"] = "video"
    elif any(w in text for w in ("either", "no preference", "doesn't matter", "does not matter")):
        update["appointment_type"] = ""

    if "today" in text:
        update["when"] = "today"
    elif "tomorrow" in text:
        update["when"] = "tomorrow"
    elif "week" in text:
        update["when"] = "this_week"

    if any(w in text for w in ("orthopedic", "ortho", "bone", "shoulder")):
        update["specialty"] = "orthopedics"
    elif any(w in text for w in ("cardio", "heart")):
        update["specialty"] = "cardiology"
    elif any(w in text for w in ("any doctor", "any available", "don't mind", "do not mind", "family medicine", "general")):
        update["specialty"] = ""
    elif conv.specialty is None:
        for token in _FALLBACK_SPECIALTIES:
            if token in text:
                update["specialty"] = "orthopedics" if token.startswith(("orthopedic", "ortho", "bone", "shoulder")) else (
                    "cardiology" if token.startswith(("cardio", "heart")) else token.replace("_", " ")
                )
                break
    return update


def _describe_update(update: dict[str, Any]) -> str | None:
    """Human phrase for what the assistant understood this turn."""
    bits: list[str] = []
    specialty = update.get("specialty")
    if specialty:
        bits.append(f"a {specialty} doctor")
    elif specialty == "":
        bits.append("any available doctor")
    if update.get("when"):
        bits.append(update["when"].replace("_", " "))
    if update.get("appointment_type"):
        bits.append(f"{update['appointment_type'].replace('_', ' ')} visit")
    return " and ".join(bits) if bits else None


# ----------------------------------------------------------------------
# graph nodes
# ----------------------------------------------------------------------

def _classify_node(state: GraphState) -> GraphState:
    conv, message = state["conv"], state["message"]
    update: dict[str, Any] = {}
    try:
        update = interpret_utterance(message, offered=conv.offered)
    except LLMUnavailableError:
        pass
    if not update:
        update = _fallback_interpret(conv, message)

    # A bare number while offers are on the table is a slot selection,
    # whatever the interpreter thought (cheap, reliable disambiguation).
    if conv.offered and not update.get("slot_selected"):
        stripped = message.strip()
        if stripped.isdigit() and 1 <= int(stripped) <= len(conv.offered):
            update["slot_selected"] = conv.offered[int(stripped) - 1]["start_at"]

    question = conv.apply_utterance(update)
    described = _describe_update(update)
    reply_prefix = f"Got it — {described}. " if described else ""
    return {
        **state,
        "update": update,
        "question": question,
        "reply": reply_prefix,
    }


def _route_after_classify(state: GraphState) -> str:
    if state["conv"].offered_slot:
        return "book"
    if state["question"]:
        return "collect_details"
    return "present_availability"


def _collect_details_node(state: GraphState) -> GraphState:
    reply = (state.get("reply") or "") + state["question"]
    return {**state, "reply": reply, "meta": {**state.get("meta", {}), "next_action": "collect_details"}}


def _present_availability_node(state: GraphState) -> GraphState:
    conv, db, user = state["conv"], state["db"], state["user"]
    try:
        search = capabilities.search_availability(
            db,
            user,
            specialty=conv.specialty or None,
            when=conv.when,
            appointment_type=conv.appointment_type,
        )
    except capabilities.AgentCapabilityError as exc:
        reason = REASON_MESSAGES.get(exc.reason, "I couldn't search availability right now.")
        return {
            **state,
            "reply": reason,
            "meta": {**state.get("meta", {}), "next_action": "error", "error_reason": exc.reason},
        }

    if not search["offers"]:
        specialty = search["specialty"] or "that"
        lines = [f"I couldn't find {specialty} availability {search['when'].replace('_', ' ')}."]
        if search["available_specialties"]:
            lines.append("Right now I can offer: " + ", ".join(search["available_specialties"]) + ".")
        lines.append("Would you like me to try another specialty or week?")
        return {**state, "reply": (state.get("reply") or "") + "\n".join(lines),
                "meta": {**state.get("meta", {}), "next_action": "no_availability"}}

    conv.hospital_id = search["offers"][0]["hospital_id"]
    conv.offered = search["offers"]
    lines = [state.get("reply") or "", "Here's what I found" +
             (f" {search['when'].replace('_', ' ')}" if search["when"] else "") + ":"]
    for i, offer in enumerate(search["offers"], start=1):
        when = offer["start_at"][:16].replace("T", " ")
        lines.append(f"{i}. {offer['doctor_name']} ({offer['specialty']}) — {when}")
    lines.append("Which one would you like? (Reply with the number, or the time.)")
    return {**state, "reply": "\n".join(part for part in lines if part),
            "meta": {**state.get("meta", {}), "next_action": "present_availability"}}


def _book_node(state: GraphState) -> GraphState:
    conv, db, user = state["conv"], state["db"], state["user"]
    meta = dict(state.get("meta", {}))
    offer = next((o for o in conv.offered if o["start_at"] == conv.offered_slot), None)
    if offer is None:
        conv.offered_slot = None
        return {**state, "reply": "Sorry — which slot would you like? Pick a number from the list.",
                "meta": {**meta, "next_action": "collect_details"}}

    try:
        result = capabilities.book_appointment(
            db,
            user,
            doctor_id=offer["doctor_id"],
            start_at=conv.offered_slot,
            appointment_type=conv.appointment_type,
            reason=None,
            correlation_id=state.get("correlation_id"),
        )
    except capabilities.AgentCapabilityError as exc:
        conv.offered_slot = None
        prefix = REASON_MESSAGES.get(exc.reason, "I couldn't book that slot.")
        meta.update({"next_action": "error", "error_reason": exc.reason})
        # Availability-class failures: immediately re-present REAL open slots.
        if exc.reason in ("slot_already_booked", "doctor_inactive", "hospital_not_approved",
                          "slot_blocked", "doctor_on_leave", "outside_working_hours"):
            try:
                search = capabilities.search_availability(
                    db, user, specialty=conv.specialty or None,
                    when=conv.when, appointment_type=conv.appointment_type,
                )
                conv.offered = search["offers"]
                if search["offers"]:
                    conv.hospital_id = search["offers"][0]["hospital_id"]
                    lines = [prefix]
                    for i, o in enumerate(search["offers"], start=1):
                        lines.append(f"{i}. {o['doctor_name']} ({o['specialty']}) — {o['start_at'][:16].replace('T', ' ')}")
                    lines.append("Which one would you like?")
                    meta.update({"next_action": "present_availability"})
                    return {**state, "reply": "\n".join(lines), "meta": meta}
            except capabilities.AgentCapabilityError:
                pass
        return {**state, "reply": prefix, "meta": meta}

    conv.appointment_id = result["appointment_id"]
    conv.ehr_outcome = result["ehr_sync_status"]
    meta.update({
        "next_action": "booked",
        "appointment_id": result["appointment_id"],
        "ehr_sync_status": result["ehr_sync_status"],
    })
    when = result["start_at"][:16].replace("T", " ")
    reply = f"Booked! {offer['doctor_name']} ({offer['specialty']}) on {when}."
    if result["ehr_sync_status"] == "synced":
        reply += " It's confirmed and synchronized with the hospital EHR."
    elif result["ehr_sync_status"] == "unknown":
        reply += (" I've sent it to the hospital system and I'm confirming the outcome — "
                  "I'll verify before calling it final.")
    elif result["ehr_sync_status"] == "failed":
        reply += " The hospital system rejected the sync; our staff will follow up."
    else:
        reply += " EHR synchronization wasn't attempted for this appointment yet."

    # Phase 6: transition into conversational questionnaire collection when the
    # booking path assigned pre-visit forms. Collection failure never unbooks.
    try:
        pending = capabilities.start_questionnaire(db, user, result["appointment_id"])
    except capabilities.AgentCapabilityError:
        pending = None
    if pending:
        conv.questionnaire = {
            "assignment_id": pending["assignment_id"],
            "template_name": pending["template_name"],
            "question_index": pending["question"].get("index", 0),
            "question_key": pending["question"]["key"],
        }
        reply += _questionnaire_intro(pending)
        meta.update({"next_action": "questionnaire", "questionnaire": conv.questionnaire})
    return {**state, "reply": reply, "meta": meta}


def _questionnaire_intro(pending: dict) -> str:
    """Human prompt for the first pending questionnaire question."""
    question = pending["question"]
    template = pending.get("template_name") or "pre-visit form"
    prompt = question.get("prompt", "")
    options = question.get("options")
    suffix = f" (Options: {', '.join(options)}.)" if options else ""
    required = "" if question.get("required", False) else " You can also skip this one."
    return f"\n\nBefore your visit I'll ask a few quick questions ({template}). First: {prompt}{suffix}{required}"


# ----------------------------------------------------------------------
# graph assembly (module-level singleton — nodes are stateless)
# ----------------------------------------------------------------------

def _build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("classify", _classify_node)
    graph.add_node("collect_details", _collect_details_node)
    graph.add_node("present_availability", _present_availability_node)
    graph.add_node("book", _book_node)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        _route_after_classify,
        {"collect_details": "collect_details", "present_availability": "present_availability", "book": "book"},
    )
    graph.add_edge("collect_details", END)
    graph.add_edge("present_availability", END)
    graph.add_edge("book", END)
    return graph.compile()


_GRAPH = _build_graph()


# ----------------------------------------------------------------------
# Phase 6: conversational questionnaire collection
# ----------------------------------------------------------------------

def _coerce_choice(question: dict, message: str):
    """Map a free-text reply onto a choice option / boolean / scale value.

    Deterministic and conservative: exact or case-insensitive option match,
    yes/no words for booleans, an integer 1–10 for scales. Anything else
    returns the raw trimmed text (the service will reject it if invalid) —
    the agent never invents an answer.
    """
    kind = question.get("kind")
    text = message.strip()
    lowered = text.lower()
    options = question.get("options") or []
    if kind in ("single_choice", "multiple_choice") and options:
        for option in options:
            if lowered == option.lower():
                return option
        for option in options:
            if lowered and (lowered in option.lower() or option.lower() in lowered):
                return option
        return text  # invalid — service rejects with a clear message
    if kind == "boolean":
        if lowered in ("yes", "y", "yeah", "yep", "true", "sure", "ok"):
            return True
        if lowered in ("no", "n", "nope", "false", "not really"):
            return False
        return text
    if kind == "scale_1_10":
        if lowered.isdigit() and 1 <= int(lowered) <= 10:
            return int(lowered)
        return text
    return text


def _questionnaire_intro(pending: dict) -> str:
    """Human prompt for the first pending questionnaire question."""
    question = pending["question"]
    template = pending.get("template_name") or "pre-visit form"
    prompt = question.get("prompt", "")
    options = question.get("options")
    suffix = f" (Options: {', '.join(options)}.)" if options else ""
    required = "" if question.get("required", False) else " You can also skip this one."
    return f"\n\nBefore your visit I'll ask a few quick questions ({template}). First: {prompt}{suffix}{required}"


def _questionnaire_turn(db, user, state: ConversationState, message: str):
    """One collection turn: record the answer to the current question, then ask
    the next one — or complete the form. Errors re-ask the same question."""
    conv = state
    meta: dict[str, Any] = {}
    qstate = dict(conv.questionnaire or {})
    assignment_id = qstate.get("assignment_id")
    question_key = qstate.get("question_key")

    # The answer the patient just typed applies to question_key. Resolve what
    # value to store: for choice questions coerce onto a listed option.
    pending = capabilities.next_pending_questionnaire_for_assignment(db, user, assignment_id)
    question = (pending or {}).get("question") or {}
    value = _coerce_choice(question, message) if question else message.strip()

    try:
        result = capabilities.answer_questionnaire(
            db, user, assignment_id=assignment_id, key=question_key, value=value
        )
    except capabilities.AgentCapabilityError as exc:
        if exc.reason in ("assignment_not_found", "questionnaire_closed", "forbidden"):
            conv.questionnaire = None
            return ("I couldn't continue with that form — it may be closed already. "
                    "Your appointment is still booked."), {
                        "next_action": "booked", "appointment_id": conv.appointment_id,
                        "questionnaire_error": exc.reason,
                    }
        # Validation problem: re-ask the SAME question, echoing the rule.
        prompt = question.get("prompt", "Could you answer that again?")
        options = question.get("options")
        suffix = f" (Options: {', '.join(options)}.)" if options else ""
        return f"Sorry — {exc.message}\n{prompt}{suffix}", {
            "next_action": "questionnaire", "questionnaire": conv.questionnaire,
        }

    if result.get("status") == "completed":
        conv.questionnaire = None
        reply = ("Thank you — that's everything I need. Your answers are with "
                 "your care team, and your appointment is booked.")
        # Another form pending for the same appointment? Chain into it.
        following = capabilities.next_pending_questionnaire(db, user, conv.appointment_id)
        if following:
            conv.questionnaire = {
                "assignment_id": following["assignment_id"],
                "template_name": following["template_name"],
                "question_index": following["question"].get("index", 0),
                "question_key": following["question"]["key"],
            }
            reply += _questionnaire_intro(following)
        meta.update({
            "next_action": "questionnaire_completed" if not conv.questionnaire else "questionnaire",
            "appointment_id": conv.appointment_id,
        })
        return reply, meta

    follow = result.get("question") or {}
    conv.questionnaire = {
        "assignment_id": result["assignment_id"],
        "template_name": (conv.questionnaire or {}).get("template_name"),
        "question_index": follow.get("index", 0),
        "question_key": follow.get("key", ""),
    }
    progress = f" ({result.get('answered_count', 0)}/{result.get('total_count', 0)} answered)"
    options = follow.get("options")
    suffix = f" (Options: {', '.join(options)}.)" if options else ""
    meta.update({"next_action": "questionnaire", "questionnaire": conv.questionnaire})
    return f"Thanks!{progress} Next: {follow.get('prompt', '')}{suffix}", meta


def run_turn(
    db: Session,
    user: User,
    state: ConversationState,
    message: str,
    *,
    correlation_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Advance the conversation one turn through the graph.

    Returns (assistant_reply, meta); meta may carry next_action,
    appointment_id, ehr_sync_status or error_reason for the service layer.
    """
    if state.appointment_id and not state.questionnaire:
        # Conversation already concluded — report state, never re-enter the flow.
        reply = (f"You're all set — appointment {state.appointment_id} "
                 f"({state.offered_slot or 'the selected slot'}) is booked.")
        if state.ehr_outcome == "synced":
            reply += " It's confirmed and synchronized with the hospital EHR."
        elif state.ehr_outcome == "unknown":
            reply += (" I'm still confirming it with the hospital system; "
                      "I'll verify before calling it confirmed.")
        elif state.ehr_outcome == "failed":
            reply += " The hospital system sync needs attention; our staff will follow up."
        return reply, {"next_action": "booked", "appointment_id": state.appointment_id}

    if state.appointment_id and state.questionnaire:
        # Phase 6: conversational questionnaire collection mode — each turn
        # records one answer and asks the next (or completes the form).
        return _questionnaire_turn(db, user, state, message)

    result = _GRAPH.invoke(
        {
            "db": db,
            "user": user,
            "conv": state,
            "message": message,
            "correlation_id": correlation_id,
            "update": {},
            "question": None,
            "reply": "",
            "meta": {},
        }
    )
    return result["reply"], dict(result.get("meta") or {})
