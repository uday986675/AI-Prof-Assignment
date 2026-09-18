# Integration Architecture — Phase 3 (Mock EHR)

> **Phase 4 status:** the retry / reconciliation / recovery *workflow* is NOT
> implemented yet. This phase deliberately ships only the foundation: typed
> connector errors, outcome recording (`synced | failed | unknown`), persistent
> idempotency, and verification lookup endpoints. Phase 4 will consume them.

## 1. Big picture

```
AI agent (Phase 5) ──► Capabilities ──► SchedulingService        (platform DB)
                                            │
                                            ▼
                                    EHRSyncService          (platform DB)
                                            │  depends only on the interface
                                            ▼
                                     EHRConnector (Protocol)
                                            │
                                            ▼
                                   MockEHRConnector (httpx)  ← the ONLY HTTP caller
                                            │  X-API-Key, Idempotency-Key,
                                            │  X-Correlation-ID
                                            ▼
                                     Mock EHR FastAPI app    (SEPARATE DB)
                                   faults middleware → EHRService → ehr.db
```

- `backend/app/integrations/ehr/base.py` — `EHRConnector` Protocol, dataclasses
  (`EHRPatient`, `EHRAppointment`), typed error taxonomy, `build_ehr_connector()`
  factory (`EHR_MODE=mock` selects the HTTP implementation).
- `backend/app/integrations/ehr/mock_ehr.py` — the only module that knows the
  Mock EHR's HTTP surface. One attempt per call; **no retry policy lives here**
  (retries are a Phase 4 workflow concern).
- `backend/app/services/__init__.py` — `EHRSyncService`: patient upsert +
  idempotent appointment creation + outcome bookkeeping + audit.
- `mock_ehr/` — fully separate FastAPI app with its own config, models,
  database, API-key security, and fault-injection middleware.

The AI agent (Phase 5) will receive *capabilities* that call platform services.
It will never import the connector, never see HTTP, never touch either database.

## 2. Database separation

| Service | Env var | Default |
|---|---|---|
| Platform | `DATABASE_URL` | `sqlite:///./data/platform.db` |
| Mock EHR | `MOCK_EHR_DATABASE_URL` | `sqlite:///./data/mock_ehr.db` |

Two independent engines/sessions (`backend/app/database/base.py` vs
`mock_ehr/app/database.py`). The EHR owns four tables: `ehr_providers`,
`ehr_patients`, `ehr_appointments`, `idempotency_records`. No table names or
metadata are shared; a test asserts the metadata sets are disjoint.

## 3. API-key authentication

The EHR requires `X-API-Key: <MOCK_EHR_API_KEY>` on every `/ehr/*` route
(constant-time comparison; missing/invalid → 401). `/health` is public.
The key lives only in the environment (`.env`, never committed).

## 4. Idempotency design

- The platform derives a **stable key per appointment**:
  `ehr_idempotency_key = "plat-appt-<appointment_id>"`, persisted on the
  appointment row and reused for every attempt. A retry can therefore never
  create a second EHR record.
- The EHR persists an `IdempotencyRecord` (unique key + SHA-256 request hash +
  result external ID). Replays return the original appointment with
  `X-EHR-Idempotent-Replay: true` and HTTP 200 (first call: 201).
- Same key with a *different* payload → 409 (protects against stale replays).
- Independent second guard: `appointments.source_platform_ref` (the platform
  appointment ID) is **unique** in the EHR — even a brand-new key for the same
  platform appointment dedupes to the existing record.
- Concurrent creators are handled by the unique indexes + rollback-and-reread.

## 5. Fault injection (deterministic, dev-only)

Controls (all default OFF, and header overrides are ignored in production mode):

| Var / header | Values | Effect |
|---|---|---|
| `MOCK_EHR_FAULT_MODE` | `server_error` | immediate 500, nothing processed |
| | `rejected` | immediate 422, nothing processed |
| | `timeout` | **process the write first, then stall** the response `MOCK_EHR_FAULT_DELAY_SECONDS` |
| | `delay` | process, then stall up to 2s |
| `MOCK_EHR_FAULT_DELAY_SECONDS` | float | stall duration |
| `MOCK_EHR_FAULT_PATHS` | comma-separated paths | target endpoints, e.g. `/ehr/appointments` (empty = all) |
| header `X-EHR-Fault` | same values | per-request override (direct EHR calls only — the platform connector forwards only its own controlled headers) |

`timeout` mode processes the request **before** stalling on purpose: when the
client (platform) times out, the appointment still exists at the EHR — the
true "unknown outcome" situation Phase 4 must reconcile. (Stalling *before*
processing would instead model a request that never arrived, because uvicorn
cancels the in-flight handler when the client disconnects.)

## 6. Error taxonomy and outcomes

Connector errors (`backend/app/integrations/ehr/base.py`):

| Error | Meaning | Sync outcome |
|---|---|---|
| `EHRAuthError` | key rejected | `failed` (config problem) |
| `EHRValidationError` | EHR refused the payload | `failed` (do NOT blind-retry) |
| `EHRNotFoundError` | referenced record absent | `failed` |
| `EHRServerError` | 5xx from the EHR | `failed` |
| `EHRUnavailableError` / `EHRUnknownOutcomeError` | timeout / network loss | **`unknown`** — the write may exist |

Every attempt appends an `IntegrationOperation` row: operation, connector,
outcome (`success | failed | unknown`), error code/detail, request payload,
response summary, idempotency key, correlation ID, duration. Appointment rows
carry `ehr_sync_status`, `external_ehr_appointment_id`, `ehr_idempotency_key`,
`ehr_synced_at`. The platform never treats a non-`synced` appointment as
EHR-confirmed.

## 7. Verification endpoints (Phase 4 building blocks)

- Platform: `POST /appointments/{id}/sync-ehr`, `GET /appointments/{id}/ehr-status`,
  `GET /integrations/operations` (tenant-scoped).
- Mock EHR: `GET /ehr/appointments/by-platform-ref/{platform_ref}` — the
  reconciliation lookup that answers "did the write land?" without creating
  anything.

## 8. What Phase 4 adds (not implemented yet)

1. Booking pipeline: revalidate slot → internal `pending_external` state →
   EHR create → classify outcome → verify → synchronize → confirm.
2. Unknown-outcome recovery: query `by-platform-ref`; if found, adopt the
   external ID and confirm; if not found, safe retry with the SAME key.
3. Reconciliation job for stale `unknown` rows; user-facing messaging rules
   ("we're confirming your booking — never double-confirm before verification").
