# Failure Recovery Design — Phase 4 (Booking Reliability & Unknown-Outcome Recovery)

> **Status:** Phase 4 is implemented and tested (see `tests/test_phase4_recovery.py`).
> The AI agent does not exist yet (Phase 5); recovery is exposed as service +
> admin endpoints and is deliberately independent of any future AI layer.

## 1. Failure categories

Every outbound EHR call returns one of three outcomes, recorded on the
appointment (`ehr_sync_status`) and in an `IntegrationOperation` row:

| Outcome    | Meaning                                                             | Typical causes                                  |
|------------|---------------------------------------------------------------------|--------------------------------------------------|
| `synced`   | The EHR record exists, was read back, and matches the platform state | Normal flow, idempotent replay, adopted reconciliation |
| `failed`   | The EHR **definitively refused**; nothing was written               | 4xx/5xx, validation rejection, auth failure      |
| `unknown`  | The request **may or may not** have been processed                   | Timeout, connection reset, network unreachable   |

The connector's typed exception hierarchy (`integrations/ehr/base.py`) drives
the classification:

- `EHRAuthError` / `EHRValidationError` / `EHRServerError` → **definitive** → `failed`
- `EHRUnknownOutcomeError` (timeout / transport error) → **unknown** → `unknown`
- Verification disagreement (EHR answered but with unexpected content) → definitive → `failed`

**The rule that matters:** a timeout is never treated as a definitive failure.
"The response was lost" and "the EHR refused" are different worlds with
different recovery actions.

## 2. Definitive failure vs unknown outcome

```
DEFINITIVE FAILURE                     UNKNOWN OUTCOME
  EHR answered: "no"                    EHR never answered
  → ehr_sync_status = failed            → ehr_sync_status = unknown
  → error_code = ehr_server_error/…     → error_code = ehr_unknown_outcome
  → nothing exists at the EHR           → the write MAY exist at the EHR
  → retry is safe any time              → NEVER blind-retry the create
  → safe retry uses the SAME key        → first LOOK UP, then adopt or retry
```

## 3. Idempotency strategy

- Key shape: `plat-appt-<appointment_id>` — deterministic, stable forever.
- Persisted on the appointment (`ehr_idempotency_key`) at the first attempt;
  every retry and every reconciliation reuses the exact same key.
- The Mock EHR persists `IdempotencyRecord` rows (unique key) so replays
  return the original record (`X-EHR-Idempotent-Replay: true`) instead of
  creating a duplicate; a replay with a *different* payload → 409 conflict.
- Second line of defense: `POST /ehr/appointments` dedupes on
  `source_platform_ref` across different keys.
- Recovery never generates a new key. A retried create that races with an
  actually-processed original request replays (no duplicate), and the
  platform verifies before claiming success.

## 4. Reconciliation algorithm

`EHRRecoveryService.reconcile_appointment(appointment_id)`:

```
load appointment (tenant-scoped by the route layer)
├─ status == cancelled                → reject (nothing to recover)
├─ ehr_sync_status == synced          → return as-is (no EHR calls at all)
├─ ehr_sync_status == unknown:
│    STEP 1  GET /ehr/appointments/by-platform-ref/<appointment_id>
│    ├─ EHR unreachable → stay unknown, log ehr.reconcile_appointment=unknown
│    ├─ FOUND, status matches  → ADOPT: store external ID, verify, synced
│    ├─ FOUND, status mismatch → failed (ehr_reconciliation_mismatch)
│    └─ NOT FOUND              → SAFE RETRY with the SAME idempotency key
│                                  (EHR-side idempotency absorbs any race),
│                                  then verify → synced
└─ ehr_sync_status in (None, failed)  → plain retry with the SAME key + verify
```

Properties:

- **Idempotent**: calling it twice is safe; an already-recovered appointment is
  recognized and returned without another create.
- **No duplicates**: adoption never creates; the retry path is protected by the
  EHR's persistent idempotency record and platform-ref dedupe.
- **Verified**: every path to `synced` re-reads the EHR record and checks its
  status before the platform claims success.

`EHRRecoveryService.sweep_unknown(hospital_id=…)` fans out over every
appointment currently in `unknown` (tenant-scoped for hospital admins). It is
exposed as `POST /integrations/reconcile-unknown`. There is intentionally **no
background scheduler yet** — that is a natural Phase 5+ addition; the service
layer already implements everything a scheduler would call.

## 5. State transitions

Appointment lifecycle (`status`) and EHR synchronization (`ehr_sync_status`)
are deliberately separate columns:

```
status:          booked → cancelled | completed | no_show
ehr_sync_status: (none) → synced | failed | unknown

EHR_SYNC_TRANSITIONS:
  None     → synced | failed | unknown
  unknown  → synced | failed | unknown        (NOT cancelled)
  failed   → synced | failed | unknown
  synced   → synced                             (terminal; reached only after verification)
```

Guards enforced in code:

- `EHRRecoveryService` / `EHRSyncService._set_ehr_status` refuse illegal moves
  (`unknown → cancelled` cannot happen through the sync dimension).
- `SchedulingService.cancel_appointment` **refuses to cancel** an appointment
  whose EHR outcome is still `unknown` (`409 ehr_outcome_unknown`): cancelling
  would strand a live EHR appointment. Reconcile first; cancellation can then
  propagate to a known EHR state.
- `synced` is only ever set after `connector.get_appointment` (or the
  by-platform-ref lookup) confirms the record exists with the expected status.

## 6. Retry safety

| Situation                                | Action                                   | Duplicate possible? |
|------------------------------------------|------------------------------------------|---------------------|
| `failed` (definitive refusal)            | retry create, same key                   | No — nothing was written |
| `unknown`, lookup finds record           | adopt, no create                         | No |
| `unknown`, lookup empty                  | retry create, same key                   | No — key replay + platform-ref dedupe |
| `unknown`, lookup also times out         | stay unknown, log, try again later       | — |
| already `synced`                         | no-op                                    | No |

## 7. Correlation IDs

The first sync attempt stamps `appointments.correlation_id`; every later
attempt and every reconciliation reuses it, so the whole story is one query:

```
ehr.create_appointment   unknown   ehr_unknown_outcome   corr 98dfcf09…
ehr.reconcile_appointment unknown  ehr_unknown_outcome   corr 98dfcf09…
ehr.reconcile_appointment success  —                     corr 98dfcf09…   (adopted)
```

The connector forwards `X-Correlation-ID` to the EHR, which stores it on the
appointment row — the same ID is visible from both sides.

## 8. Integration operation logging

Every attempt writes an `IntegrationOperation` (`backend/app/models/integration.py`)
with: `operation` (`ehr.create_appointment` / `ehr.reconcile_appointment`),
`connector`, `outcome`, `error_code`, `error_detail`, `resource_type`,
`resource_id`, `idempotency_key`, `correlation_id`, `duration_ms`,
`request_payload` (no PHI beyond what the EHR already receives) and
`response_summary`. Routes expose the log at `GET /integrations/operations`,
tenant-scoped (hospital admin: own hospital only; platform admin: all;
patients/doctor roles: 403).

## 9. Why the AI must never perform recovery directly

- **Correctness under uncertainty** demands one code path: the reconciliation
  algorithm above is deterministic, verified, and race-safe. An LLM deciding
  "should I retry?" would reintroduce exactly the duplicate-appointment risk
  the design removes.
- **Auditability**: every recovery is a logged, attributable service operation;
  an agent improvising calls would be untraceable.
- **Authorization**: recovery is tenant-scoped and RBAC-checked at the API
  boundary; an autonomous agent bypassing that boundary would break Phase 1
  isolation guarantees.

The Phase 5 AI agent will *observe* sync state through controlled capability
results ("your booking is confirmed" only when `ehr_sync_status == synced`) and
*request* recovery through `EHRRecoveryService` — it will never see the
database, the connector, or the EHR.
