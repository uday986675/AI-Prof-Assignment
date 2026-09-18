# AI Agent — Phase 5 Architecture

> Status: **implemented and tested** (28 dedicated tests; suite total 134).
> The agent can search REAL availability, book through the controlled
> capability path, trigger EHR synchronization, and report the recorded
> outcome (`synced | failed | unknown`) honestly. It NEVER touches the
> database, the connector, or the Mock EHR directly.

## 1. Where the agent sits

```
Patient (text/voice UI in later phases)
    │  HTTPS + JWT (patient role)
    ▼
/api/agent/sessions/{id}/messages        (thin routes, backend/app/api/routes/agent.py)
    ▼
AgentService                             (backend/app/agent/service.py)
    │  loads/saves ConversationState checkpoint (platform DB)
    ▼
LangGraph StateGraph                     (backend/app/agent/graph.py)
    │  classify → collect_details | present_availability | book
    ▼
capabilities                             (backend/app/agent/capabilities.py)  ← THE BOUNDARY
    │                         │
    ▼                         ▼
SchedulingService        EHRSyncService / EHRRecoveryService
(Phase 2 engine)         (Phases 3–4: idempotency, verification,
    │                      unknown-outcome reconciliation)
    ▼                         ▼
platform DB              EHRConnector → Mock EHR (separate process + DB)
```

Hard rules enforced by the module layout:

- `backend/app/agent/*` imports **no** HTTP client, **no** SQLAlchemy writes
  outside the conversation tables, and **no** `mock_ehr` code.
- The only tools the graph can call live in `capabilities.py`, which delegates
  to the same services the REST API uses — revalidation, the partial unique
  index against double-booking, stable idempotency keys, correlation IDs,
  RBAC and tenant isolation are inherited, not reimplemented.
- The LLM is an **input translator only**. It converts one utterance into a
  JSON update (`specialty / when / appointment_type / slot_selected`); the
  slot-filling state machine (`state.py`) validates every value, so a
  hallucinated field can never reach booking.

## 2. Conversation model

`AgentConversation` (table `conversations`): owner (`user_id`), optional
`hospital_id` context, `status` (`active | booked | abandoned`) and a JSON
`state` checkpoint — the `ConversationState` dataclass. The checkpoint makes
conversations restart-safe: a patient can continue in a new tab/process and the
agent resumes with the same specialty, offers, and pending selection.

`ConversationEvent` (table `conversation_events`): append-only turn log, one
row per user message and per agent reply, with `meta` (e.g. `next_action`,
`appointment_id`, `ehr_sync_status`). `created_at` uses the same strictly
monotonic clock guard as `IntegrationOperation`, so replays are deterministic
even for back-to-back writes on coarse Windows clocks.

## 3. The turn graph

```
        START
          │
      classify ──────────────► collect_details → END   (ask the missing question)
        │    │
        │    └────────────────► present_availability → END
        │                          (search REAL slots; list offers)
        └──────────────────────► book → END
                                   (slot picked → capability booking + EHR sync)
```

Routing (`_route_after_classify`):
- a slot was selected → **book**;
- a mandatory answer is still missing → **collect_details** (asks the visit-type
  question first, then specialty);
- otherwise → **present_availability**.

Failure routing inside `book`: `slot_already_booked`, `doctor_inactive`,
`hospital_not_approved`, `slot_blocked`, `doctor_on_leave`,
`outside_working_hours` immediately trigger a fresh availability search and
re-present open slots — the conversation never dead-ends after a conflict.

## 4. LLM layer and the deterministic fallback

`llm.py` resolves the chat model: **Groq first** (`GROQ_API_KEY`,
`GROQ_MODEL`), **Gemini as fallback** (`GEMINI_API_KEY`, `GEMINI_MODEL`),
`temperature=0`, strict JSON-only instruction. `interpret_utterance` is
best-effort: any provider error or malformed output yields `{}`.

When the LLM is absent/unreachable/unhelpful, `_fallback_interpret` maps
keywords ("shoulder", "heart", "video", "tomorrow", "this week", …) over the
same controlled vocabulary. The fallback can only ASK or SEARCH — it never
invents a booking. Production behavior with no keys configured is therefore a
fully working (less chatty) assistant, which is also what the tests exercise.

## 5. Honest EHR reporting

`capabilities.book_appointment` books via `SchedulingService`, then triggers
`EHRSyncService.sync_appointment` exactly like `POST /appointments/{id}/sync-ehr`.
The reply text is derived from the RECORDED outcome:

| `ehr_sync_status` | Patient hears |
|---|---|
| `synced` | confirmed and synchronized with the hospital EHR |
| `unknown` | sent to the hospital system, outcome being confirmed before calling it final |
| `failed` | hospital system rejected the sync; staff will follow up |
| `not_attempted` | EHR synchronization wasn't attempted yet |

`unknown` is resolved by the existing Phase 4 flow
(`POST /appointments/{id}/reconcile-ehr` — adopt if the EHR record exists, safe
retry with the same `plat-appt-<id>` key otherwise). The agent itself never
retries, never guesses, and never claims an unverified confirmation.

## 6. API surface

| Endpoint | Auth | Behavior |
|---|---|---|
| `POST /agent/sessions` | patient | new conversation, empty checkpoint |
| `GET /agent/sessions` | patient | caller's conversations, newest first |
| `GET /agent/sessions/{id}` | owner or platform admin | conversation + full turn log |
| `POST /agent/sessions/{id}/messages` | patient (owner) | one conversational turn |

Errors: `401` no/invalid token; `403` non-patient role on session create; `404`
foreign/nonexistent conversation (existence hidden); `422` empty/oversized
message. Booking failures surface as conversational re-presentation, not 5xx.

## 7. What Phase 5 deliberately does NOT do

- No questionnaires (Phase 6), no voice I/O (Phase 7), no dashboards (Phase 8).
- No background scheduler for the Phase 4 sweep (service API is ready for one).
- No cancellation/reschedule through the chat (a missed-slot offer is simply
  re-booked as a new appointment; cancellations remain a REST action).
