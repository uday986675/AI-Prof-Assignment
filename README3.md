# README3 — Phase 3 Execution Guide (Mock EHR + Synchronization)

Runbook for everything built in **Phase 3**. Every command was executed live
against both services before this guide was written. See also:
`README.md` (project overview) · `README2.md` (Phase 1–2 walkthrough) ·
`docs/integration.md` (architecture deep-dive).

> **Phase 4 not included:** sync outcomes can be `synced | failed | unknown`,
> and the unknown-outcome *recovery workflow* (automatic reconciliation) is
> intentionally NOT implemented yet — this guide shows the state Phase 4 will
> resolve.

---

## 0. One-time setup (if not already done)

```bash
# from the repo root (Windows Git Bash shown; adjust for your shell)
python -m venv .venv && source .venv/Scripts/activate   # recommended when PyPI is reachable
pip install -r requirements.txt
cp .env.example .env        # then edit: set JWT_SECRET and MOCK_EHR_API_KEY
```

Default `.env` values work out of the box for both services
(`DATABASE_URL=sqlite:///./data/platform.db`, `MOCK_EHR_DATABASE_URL=sqlite:///./data/mock_ehr.db`,
`MOCK_EHR_URL=http://127.0.0.1:8001`).

## 1. Seed both databases (idempotent)

```bash
python -m backend.scripts.seed_demo     # platform: hospitals, doctors, patient
python -m mock_ehr.scripts.seed_demo    # EHR: providers EHR-PROV-001..003
```

## 2. Start both services (two terminals)

```bash
# Terminal 1 — Mock EHR (port 8001; must match MOCK_EHR_URL in .env)
python -m uvicorn mock_ehr.app.main:app --host 127.0.0.1 --port 8001

# Terminal 2 — Platform API (port 8002 here, to keep 8000 free for other uses)
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8002
```

> ⚠️ The platform calls whatever `MOCK_EHR_URL` says. If the EHR is NOT running
> there, syncs return `"ehr_sync_status": "unknown"` (network loss semantics) —
> by design. Check both health endpoints first:

```bash
curl http://127.0.0.1:8001/health   # {"service":"mock_ehr","status":"ok",...}
curl http://127.0.0.1:8002/health   # {"status":"ok","service":"platform-api",...}
```

Set the API base for the walkthrough:

```bash
export API=http://127.0.0.1:8002
export EHR=http://127.0.0.1:8001
export EHR_KEY=<your MOCK_EHR_API_KEY from .env>
```

Login helper:

```bash
tok() { curl -s -X POST $API/auth/login -H "Content-Type: application/json" \
  -d "{\"email\":\"$1\",\"password\":\"Demo1234!\"}" \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])"; }
PAT=$(tok patient@demo.health); ADMIN=$(tok admin.a@citygeneral.health)
```

## 3. Book a slot (Phase 2 flow — unchanged)

```bash
RAO=$(curl -s $API/hospital/doctors -H "Authorization: Bearer $ADMIN" \
  | python -c "import sys,json;d=json.load(sys.stdin);print([x['id'] for x in d if x['external_provider_id']=='EHR-PROV-001'][0])")
MON=$(python -c "from datetime import date,timedelta;d=date.today()+timedelta(days=35)
while d.weekday()!=0: d+=timedelta(days=1)
print(d.isoformat())")     # Dr. Rao works Mon–Fri; +35d lands on a Monday

BOOK=$(curl -s -X POST $API/appointments \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "X-Correlation-ID: demo-corr-001" \
  -d "{\"doctor_id\":\"$RAO\",\"start_at\":\"${MON}T09:00:00\",\"reason\":\"shoulder pain evaluation\"}")
APPT_ID=$(echo "$BOOK" | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "$APPT_ID"
```

## 4. Synchronize the appointment to the Mock EHR

```bash
curl -s -X POST $API/appointments/$APPT_ID/sync-ehr -H "Authorization: Bearer $PAT"
# → {"id":"...","ehr_sync_status":"synced",
#    "external_ehr_appointment_id":"EHR-...", "ehr_idempotency_key":"plat-appt-..."}
```

What happened under the hood (one `IntegrationOperation` row records it all):
1. patient upsert at the EHR (`POST /ehr/patients`),
2. idempotent appointment create (`POST /ehr/appointments` with `Idempotency-Key: plat-appt-<id>`,
   `source_platform_ref=<platform appointment id>`, `X-Correlation-ID: demo-corr-001`),
3. external ID stored on the platform appointment.

Verify from the EHR side directly:

```bash
# by external id
curl -s $EHR/ehr/appointments/<external_ehr_appointment_id> -H "X-API-Key: $EHR_KEY"
# or by platform reference
curl -s $EHR/ehr/appointments/by-platform-ref/$APPT_ID -H "X-API-Key: $EHR_KEY"
```

Platform-side status (reads the stored reference and queries the EHR):

```bash
curl -s $API/appointments/$APPT_ID/ehr-status -H "Authorization: Bearer $PAT"
```

Admin observability:

```bash
curl -s "$API/integrations/operations?appointment_id=$APPT_ID" -H "Authorization: Bearer $ADMIN"
```

## 5. Idempotency proof — no duplicate EHR appointments

```bash
# 5a. syncing again is refused at the platform (already verified once)
curl -s -o /dev/null -w "%{http_code}\n" -X POST $API/appointments/$APPT_ID/sync-ehr \
  -H "Authorization: Bearer $PAT"           # → 409 (already_synced)

# 5b. even a RAW replay of the same create request returns the SAME record
curl -s -o /dev/null -w "%{http_code}\n" -X POST $EHR/ehr/appointments \
  -H "X-API-Key: $EHR_KEY" -H "Idempotency-Key: plat-appt-$APPT_ID" \
  -H "Content-Type: application/json" \
  -d "{\"external_patient_id\":\"PLAT-...\",\"external_provider_id\":\"EHR-PROV-001\", \
       \"start_at\":\"${MON}T09:00:00\",\"end_at\":\"${MON}T09:30:00\", \
       \"source_platform_ref\":\"$APPT_ID\"}"   # → 200 replay (first call was 201)
```

The EHR persists `IdempotencyRecord` rows (unique key, SHA-256 request hash) —
replays survive restarts, payload mismatches under the same key get 409, and
`source_platform_ref` is UNIQUE in the EHR, so no sequence of retries can
create a second appointment for one platform booking.

## 6. Failure demo A — EHR server error (definitive failure)

Stop the EHR and restart it with a fault mode:

```bash
# Terminal 1 (Ctrl+C first), then:
MOCK_EHR_FAULT_MODE=server_error python -m uvicorn mock_ehr.app.main:app --host 127.0.0.1 --port 8001
```

Book a fresh slot and sync:

```bash
# ...book another appointment (e.g. ${MON}T10:00:00) → APPT2...
curl -s -X POST $API/appointments/$APPT2/sync-ehr -H "Authorization: Bearer $PAT"
# → {"ehr_sync_status":"failed","external_ehr_appointment_id":null}
```

Check the audit trail, then recover:

```bash
curl -s "$API/integrations/operations?appointment_id=$APPT2" -H "Authorization: Bearer $ADMIN"
# → outcome:"failed", error_code:"ehr_server_error"

# Restart the EHR WITHOUT the fault mode, then simply sync again:
curl -s -X POST $API/appointments/$APPT2/sync-ehr -H "Authorization: Bearer $PAT"
# → {"ehr_sync_status":"synced","external_ehr_appointment_id":"EHR-..."}
```

Nothing was written at the EHR during the failure, so the retry (SAME stable
idempotency key) creates exactly one record.

## 7. Failure demo B — unknown outcome (the Phase 4 seed scenario)

Restart the EHR stalling **only appointment writes**:

```bash
MSYS_NO_PATHCONV=1 MOCK_EHR_FAULT_MODE=timeout MOCK_EHR_FAULT_DELAY_SECONDS=8 \
MOCK_EHR_FAULT_PATHS="/ehr/appointments" \
  python -m uvicorn mock_ehr.app.main:app --host 127.0.0.1 --port 8001
```

> ⚠️ **Git Bash users:** `MSYS_NO_PATHCONV=1` is REQUIRED — otherwise MSYS
> rewrites the path-like value `/ehr/appointments` into a Windows path and the
> targeting silently doesn't match. (PowerShell/CMD don't need it.)

Book and sync a fresh appointment (e.g. `${MON}T12:00:00` → `APPT3`):

```bash
curl -s -X POST $API/appointments/$APPT3/sync-ehr -H "Authorization: Bearer $PAT"
# → {"ehr_sync_status":"unknown","external_ehr_appointment_id":null}
#   (platform gave up after its 5s timeout — while the EHR, 8s later,
#    COMPLETED the write)
```

The truth at the EHR — the record EXISTS even though the platform never saw a
response:

```bash
curl -s $EHR/ehr/appointments/by-platform-ref/$APPT3 -H "X-API-Key: $EHR_KEY"
# → the EHR appointment, with the SAME correlation id (demo-corr-...)
```

Platform state is honestly `unknown` (never confirmed blindly), and the op log
shows `outcome:"unknown", error_code:"ehr_unknown_outcome"` with the stable
idempotency key retained. **This is exactly the state Phase 4's recovery
workflow resolves**: query by platform ref → found → adopt the external ID and
confirm; not found → safe retry with the same key.

## 8. Reset demo data between runs (optional)

```bash
rm data/platform.db data/mock_ehr.db   # then re-run both seed scripts
```

## 9. Tests

```bash
python -m unittest discover -s tests -v
# → 73 tests: 16 (Phase 1) + 25 (Phase 2) + 32 (Phase 3)
```

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| sync → `unknown` though EHR looks fine | `MOCK_EHR_URL` doesn't match the port the EHR runs on; check `$EHR/health` |
| fault env var seems ignored (no stall) | Git Bash path mangling — prefix `MSYS_NO_PATHCONV=1`; or the `MOCK_EHR_FAULT_PATHS` filter doesn't match the path you're calling |
| 401 on direct EHR curl | wrong `X-API-Key`; use the `MOCK_EHR_API_KEY` from `.env` |
| 409 `Idempotency-Key reused with a different request payload` | expected: same key, different body — the EHR protecting against stale replays |
| ports busy | change `--port`; keep `MOCK_EHR_URL` in sync with the EHR's port |

---

*Runbook verified end-to-end on 2026-09-16: booking → sync (`synced`), EHR
verification, 409 re-sync, replay dedupe, `server_error` → `failed` → safe
retry, and timeout → `unknown` with the record present at the EHR.*
