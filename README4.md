# README4 — Phase 4 Runbook: Booking Reliability & Unknown-Outcome Recovery

Every command below was executed live against both running services before
this file was delivered. Design details: `docs/failure-recovery.md`.

**What Phase 4 adds:** `POST /appointments/{id}/reconcile-ehr`,
`POST /integrations/reconcile-unknown`, EHR verification before any `synced`
claim, stable idempotency keys across retries, correlation-ID continuity, the
`ehr_outcome_unknown` cancellation guard, and full test coverage
(106 tests total).

---

## 0. Prerequisites

- Phase 1–3 setup done (`.env` present — copy from `.env.example` if not).
- Ports: Mock EHR on **8001** (must match `MOCK_EHR_URL`), platform on 8000/8002.
- Demo accounts (from `python -m backend.scripts.seed_demo`):
  `admin.a@citygeneral.health` / `patient@demo.health`, password `Demo1234!`.

## 1. Seed + start both services

```bash
python -m backend.scripts.seed_demo
python -m mock_ehr.scripts.seed_demo

# Terminal 1
python -m uvicorn mock_ehr.app.main:app --host 127.0.0.1 --port 8001
# Terminal 2
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8002
```

Health checks:

```bash
curl -s http://127.0.0.1:8001/health
curl -s http://127.0.0.1:8002/health
```

## 2. Login helper + book a slot

```bash
API=http://127.0.0.1:8002
tok() { curl -s -X POST $API/auth/login -H "Content-Type: application/json" \
  -d "{\"email\":\"$1\",\"password\":\"Demo1234!\"}" | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])"; }
ADMIN=$(tok admin.a@citygeneral.health)
PAT=$(tok patient@demo.health)

RAO_ID=$(curl -s "$API/hospital/doctors" -H "Authorization: Bearer $ADMIN" | python -c "
import sys,json; rows=json.load(sys.stdin)
rows=rows if isinstance(rows,list) else rows.get('doctors',[])
print([d['id'] for d in rows if 'Rao' in d['full_name']][0])")

MON=$(python -c "from datetime import date,timedelta;d=date.today()+timedelta(days=35);print(d+timedelta(days=(0-d.weekday())%7))")

FREE=$(curl -s "$API/doctors/$RAO_ID/availability?from_date=$MON&to_date=$MON" -H "Authorization: Bearer $PAT" | python -c "
import sys,json
for day in json.load(sys.stdin)['days']:
    for s in day['slots']:
        if s['available']: print(s['start_at']); break
    else: continue
    break")

APPT=$(curl -s -X POST $API/appointments -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d "{\"doctor_id\":\"$RAO_ID\",\"start_at\":\"$FREE\",\"appointment_type\":\"in_person\",\"reason\":\"Phase 4 demo\"}" \
  | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "booked: $APPT"
```

## 3. Scenario 1 — definitive failure (server error) → failed → retry → synced

```bash
# 3a. Restart the EHR with the fault enabled (Terminal 1: Ctrl-C first)
MOCK_EHR_FAULT_MODE=server_error python -m uvicorn mock_ehr.app.main:app --port 8001

# 3b. Sync → definitive failure
curl -s -X POST $API/appointments/$APPT/sync-ehr -H "Authorization: Bearer $PAT"
# → {"ehr_sync_status":"failed","external_ehr_appointment_id":null,
#     "ehr_idempotency_key":"plat-appt-<APPT>"}

# 3c. Restore the EHR (Ctrl-C, restart without fault vars)
python -m uvicorn mock_ehr.app.main:app --port 8001

# 3d. Reconcile → safe retry with the SAME key → verified synced
curl -s -X POST $API/appointments/$APPT/reconcile-ehr -H "Authorization: Bearer $PAT"
# → {"ehr_sync_status":"synced","external_ehr_appointment_id":"EHR-…"}
```

## 4. Scenario 2 — UNKNOWN outcome where the EHR actually processed the write (ADOPT)

```bash
# 4a. Restart the EHR: 8s stall on EXACTLY the appointment-create path.
#     The write completes BEFORE the response stalls, so the record is durable.
#     (Git Bash users: MSYS_NO_PATHCONV=1 stops /ehr/appointments being
#     rewritten into a Windows path.)
MSYS_NO_PATHCONV=1 MOCK_EHR_FAULT_MODE=timeout MOCK_EHR_FAULT_DELAY_SECONDS=8 \
MOCK_EHR_FAULT_PATHS=/ehr/appointments \
  python -m uvicorn mock_ehr.app.main:app --port 8001

# 4b. Book a FRESH appointment (repeat §2), then sync — the platform's 5s
#     connector timeout fires first:
curl -s -X POST $API/appointments/$APPT2/sync-ehr -H "Authorization: Bearer $PAT"
# → {"ehr_sync_status":"unknown", ...}   (error_code ehr_unknown_outcome lands
#   in the integration-operations log, §6)

# 4c. Wait out the 8s stall, then look at the EHR's truth:
EHR_KEY=$(grep -E '^MOCK_EHR_API_KEY=' .env | cut -d= -f2- | tr -d '\r"')
curl -s "http://127.0.0.1:8001/ehr/appointments/by-platform-ref/$APPT2" -H "X-API-Key: $EHR_KEY"
# → the appointment EXISTS at the EHR although the platform never heard back

# 4d. Reconcile → ADOPT (no second appointment is created):
curl -s -X POST $API/appointments/$APPT2/reconcile-ehr -H "Authorization: Bearer $PAT"
# → {"ehr_sync_status":"synced","external_ehr_appointment_id":"EHR-<same as 4c>"}

# 4e. Calling reconciliation again is a no-op (already recovered):
curl -s -X POST $API/appointments/$APPT2/reconcile-ehr -H "Authorization: Bearer $PAT"
# → same external ID, ehr_sync_status stays "synced"
```

## 5. Scenario 3 — UNKNOWN + NOT FOUND → safe retry (SAME idempotency key)

```bash
# 5a. Stop the EHR entirely (true network outage). Book a fresh appointment
#     (repeat §2) and sync:
curl -s -X POST $API/appointments/$APPT3/sync-ehr -H "Authorization: Bearer $PAT"
# → {"ehr_sync_status":"unknown"}

# 5b. Reconcile while the EHR is still down → stays unknown, no crash:
curl -s -X POST $API/appointments/$APPT3/reconcile-ehr -H "Authorization: Bearer $PAT"
# → {"ehr_sync_status":"unknown"}   (operation logged as unknown)

# 5c. Restart the EHR normally, reconcile again:
python -m uvicorn mock_ehr.app.main:app --port 8001
curl -s -X POST $API/appointments/$APPT3/reconcile-ehr -H "Authorization: Bearer $PAT"
# → {"ehr_sync_status":"synced"}   (lookup found nothing → safe retry, same key)

# 5d. Exactly ONE EHR record exists:
curl -s "http://127.0.0.1:8001/ehr/appointments/by-platform-ref/$APPT3" -H "X-API-Key: $EHR_KEY"
```

## 6. Audit trail: the full story under one correlation ID

```bash
curl -s "$API/integrations/operations?appointment_id=$APPT2" -H "Authorization: Bearer $ADMIN"
# ehr.create_appointment     | unknown | ehr_unknown_outcome | corr 98dfcf09…
# ehr.reconcile_appointment  | unknown | ehr_unknown_outcome | corr 98dfcf09…
# ehr.reconcile_appointment  | success | —                   | corr 98dfcf09… (adopted)
```

Hospital-admin sweep (tenant-scoped; recovers every `unknown` appointment):

```bash
curl -s -X POST $API/integrations/reconcile-unknown -H "Authorization: Bearer $ADMIN"
```

Cancellation guard (spec §12):

```bash
# Create an unknown-outcome appointment: stop the EHR, book fresh, sync.
# ($APPT4 = such an appointment; see scenario 5a for the exact steps.)
curl -s -X PATCH $API/appointments/$APPT4/cancel \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" -d '{"reason":"x"}'
# → 409 {"reason":"ehr_outcome_unknown"}
```

## 7. Tests

```bash
python -m unittest discover -s tests -v
# Phase 1: 16, Phase 2: 25, Phase 3: 32, Phase 4: 33  →  Ran 106 tests — OK
```

## 8. Reset + troubleshooting

```bash
# Reset demo data (wipes both databases, re-seeds):
python - <<'EOF'
import pathlib
for p in ("data/platform.db", "data/mock_ehr.db"):
    pathlib.Path(p).unlink(missing_ok=True)
EOF
python -m backend.scripts.seed_demo && python -m mock_ehr.scripts.seed_demo
```

| Symptom | Cause | Fix |
|---|---|---|
| Sync reports `unknown` but EHR reachable | `MOCK_EHR_URL` port ≠ EHR's port | start EHR on the configured port (8001) |
| Fault path targeting never applies (Git Bash) | MSYS rewrites `/ehr/appointments` to a Windows path | prefix env with `MSYS_NO_PATHCONV=1` |
| EHR direct reads return 401 | `.env` has CRLF endings so `cut -d= -f2` keeps `\r` | `tr -d '\r"'` when extracting the key |
| `POST /integrations/reconcile-unknown` → 403 | role not `hospital_admin`/`platform_admin` | use an admin token |
| Port already in use | previous server still running | `for pid in $(netstat -ano | grep ":8001" | grep LISTENING | awk '{print $5}'); do taskkill //PID $pid //F; done` (Windows) |
