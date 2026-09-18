# Phase 5 — AI Agent (LangGraph + capability tools) — Implementation Plan

> Written before coding (PHASE 5.2). The build follows this plan; deviations
> found during implementation are corrected here in a post-build note.

## Source of truth

- PRD workflow: patient describes need → AI clarifies → searches REAL
  availability → patient selects slot → booking through a controlled capability →
  EHR sync → verified confirmation. (EHR sync stays as delivered by Phases 3–4.)
- README status table: "Phase 5 — AI agent: LangGraph + capability tools,
  persisted conversation state."
- Spec's explicit boundaries: the AI agent must NEVER touch the database or the
  Mock EHR directly; all actions go through capabilities; scheduling stays
  independent of the AI.

## Mapping requirements → modules (all reuse Phase 1–4 abstractions)

| Requirement | Module (new) | Reuses |
|---|---|---|
| Conversation sessions + persisted state | `backend/app/models/conversation.py` | `Base`, `new_id`, `utcnow` |
| Turn log (auditability of the dialogue) | `backend/app/models/conversation.py` | AuditEvent conventions |
| Slot-filling dialogue state | `backend/app/agent/settings.py` | `APPOINTMENT_TYPES`, `APPOINTMENT_TYPES_REQUIRED` (Phase 2) |
| "Understands admin intent, asks clarification" | `backend/app/agent/graph.py` (LangGraph) | — |
| LLM with provider fallback | `backend/app/agent/llm.py` | `settings.groq_api_key` / `gemini_api_key` |
| Search REAL availability (read-only tool) | `backend/app/agent/capabilities.py` → `search_availability` | `SchedulingService.available_slots` |
| Booking (controlled capability) | `capabilities.py` → `book_appointment` | `SchedulingService.book_appointment` (revalidation + unique index kept) |
| EHR sync + recovery (system-owned, not AI) | `capabilities.py` → `sync_appointment_to_ehr` | `EHRSyncService`, `EHRRecoveryService` (Phase 4) |
| Patient-facing chat/session API | `backend/app/api/routes/agent.py` | `require_patient`, `get_current_user`, `get_db` |
| Approval, idempotency, correlation, RBAC, state safety | untouched | Phases 1–4 |
| Conversation API added to bookings? | **No** — decision recorded | keeps Phase 1–4 API stable |

## New files

1. `backend/app/models/conversation.py` — `AgentConversation`, `ConversationEvent`.
2. `backend/app/agent/__init__.py`, `state.py`, `graph.py`, `llm.py`, `capabilities.py`, `service.py`.
3. `backend/app/schemas/agent.py` — Pydantic request/response models.
4. `backend/app/api/routes/agent.py` — endpoints.
5. `tests/test_phase5_agent.py` — new unittest suite.
6. `docs/ai-agent.md`, `README5.md`; README.md status update.

## Modified files

- `backend/app/models/__init__.py` — export the two new models.
- `backend/app/main.py` — register the agent router.
- `backend/scripts/seed_demo.py` — add Monday availability for `dr.mehta@citygeneral.health`
  (currently starts Tuesday), so agent demos always have a near-term slot.
  Existing rows are NOT touched; the seed stays idempotent.

## Database changes

- Two NEW tables only (`conversations`, `conversation_events`) — created by
  `Base.metadata.create_all`; no existing table or column changes; old databases
  stay valid; fresh databases work identically.

## API changes (new endpoints only)

- `POST /agent/sessions` — start a conversation (patient role).
- `GET /agent/sessions` — list own sessions (patient role).
- `GET /agent/sessions/{id}` — one session with events (owner or platform admin).
- `POST /agent/sessions/{id}/messages` — send a patient utterance, get the assistant reply.
- Booking follow-ups (`POST /appointments/{id}/sync-ehr`, `POST /appointments/{id}/reconcile-ehr`)
  remain exactly as in Phase 3–4 (the agent capability calls the same services).

## Authorization

- Every agent endpoint requires a valid JWT (401) and the `patient` role (403).
- Sessions are owned by the creating user; owner and platform admin only
  (403 otherwise; hidden-resource convention preserved).
- Capabilities enforce the same authorization as the REST API because they
  delegate to the same services; `book_appointment` is patient-only;
  `search_availability` is read-only and scoped to approved hospitals.

## Failure behavior (inherited from Phases 3–4, surfaced verbatim)

- `slot_already_booked` → 409-equivalent structured failure in chat; the agent
  re-presents availability (state intact, nothing double-booked).
- `doctor_missing_external_provider_id` → booking fails before any EHR write;
  agent explains; nothing synchronized.
- EHR outcomes (`synced/failed/unknown`) are recorded on the appointment by the
  sync service exactly as before; the agent's confirmation message derives from
  the stored outcome and NEVER claims verified success without `synced`.
- LLM outage → deterministic fallback dialogue (conversation continues; no crash).

## Tests (new file, unittest, mirroring groups A–H of the spec)

- Unit: slot-filling state machine transitions and validation gates.
- Capabilities: happy path, slot conflict, invalid type, cross-hospital doctor hidden,
  booking → EHR sync outcome recorded, unchanged Phase 1–4 behavior.
- API: session create, 401 without token, 403 for non-patient roles,
  tenant/ownership isolation on session reads, multi-turn persistence across
  "process restarts" (new orchestrator, same DB), event ordering, message on a
  foreign session, LLM-fallback conversation, booking confirmation content.
- Suite target: existing 106 + new ≥ 15, all green via
  `python -m unittest discover -s tests -v`.

## Explicit non-goals for this phase

- Questionnaires (Phase 6), voice (Phase 7), dashboards (Phase 8).
- Auto-reschedule/cancel *policies*; the deterministic fallback keeps the same
  capabilities a human would trigger next.
