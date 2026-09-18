# Phase 2 Runbook — How to Run the System (Foundation + Scheduling)

This is the hands-on execution guide for everything built through **Phase 2**.
(If you want the project overview, architecture, and demo-account reference, see
[`README.md`](README.md). This file is the "do this, then that" guide.)

**What works right now:**

- Hospital admin: manage hospital profile, doctors, weekly availability, blocked periods/leave
- Platform admin: approve/reject/suspend hospitals, view audit trail
- Scheduling engine: real slot calculation (never invented), blocked/leave filtering,
  pre-booking revalidation, **race-safe double-booking prevention**
- Patient: search any approved hospital's doctor availability, book a slot, list/cancel own appointments
- Not yet (planned phases): Mock EHR, booking reliability pipeline, AI agent, questionnaires, voice, dashboards

---

## 1. One-time setup

From the repository root:

```bash
# (Recommended if you have internet) create a venv and install dependencies
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash; macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# Configure environment (defaults work for local dev)
cp .env.example .env               # PowerShell: copy .env.example .env
```

Edit `.env` and set at minimum:

```ini
JWT_SECRET=change-me-to-a-long-random-string
DATABASE_URL=sqlite:///./data/platform.db
```

The SQLite database file is created automatically on first run — no DB server needed.

## 2. Seed demo data

```bash
python -m backend.scripts.seed_demo
```

Expected output ends with:

```
Seed complete.
Demo accounts (password for all: Demo1234!):
  platform_admin@demo.health            Platform Admin
  admin.a@citygeneral.health            Alice Admin (City General)
  admin.b@stmary.health                 Bob Admin (St Mary)
  dr.rao@citygeneral.health             Dr. Rao
  dr.mehta@citygeneral.health           Dr. Mehta
  dr.iyer@stmary.health                 Dr. Iyer
  patient@demo.health                   Demo Patient
```

The script is idempotent — safe to run repeatedly. It creates:

- **City General Hospital** — approved, 2 active doctors (Dr. Rao: orthopedics, Mon–Fri
  09:00–13:00 & 14:00–17:00; Dr. Mehta: cardiology, Mon/Wed/Fri 10:00–13:00), plus a leave
  period for Dr. Rao starting the day after tomorrow
- **St Mary Hospital** — submitted but *not approved* (useful for testing the approval lock)

## 3. Start the API server

```bash
python -m uvicorn backend.app.main:app --reload --port 8000
```

- Swagger UI: **http://127.0.0.1:8000/docs**
- Health check: `curl http://127.0.0.1:8000/health` → `{"status":"ok","service":"platform-api",...}`

Leave this terminal running; open a second terminal for the walkthrough below.

## 4. Run the test suite

```bash
python -m unittest discover -s tests -v
```

Expected: `Ran 41 tests ... OK` (16 foundation + 25 scheduling, including the six required
scheduling scenarios and the concurrent double-booking race test).

---

## 5. Guided walkthrough (copy-paste, ~5 minutes)

All commands run in a POSIX shell (Git Bash on Windows). Every login below uses password
`Demo1234!`. We'll use a helper to grab tokens:

```bash
API=http://127.0.0.1:8000
tok() { curl -s -X POST $API/auth/login -H "Content-Type: application/json" \
        -d "{\"email\":\"$1\",\"password\":\"Demo1234!\"}" \
        | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])"; }
```

### Step A — Hospital admin configures the hospital (the top of the workflow)

```bash
ADMIN=$(tok admin.a@citygeneral.health)

# 1) My hospital profile
curl -s $API/hospital/me -H "Authorization: Bearer $ADMIN"

# 2) My tenant's doctors
curl -s $API/hospital/doctors -H "Authorization: Bearer $ADMIN"

# 3) Add a new doctor
DOC=$(curl -s -X POST $API/hospital/doctors \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"full_name":"Dr. Sharma","specialty":"orthopedics","consultation_minutes":30}')
echo "$DOC"
DOC_ID=$(echo "$DOC" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

# 4) Configure Friday evening availability: 17:00-20:00 in 30-minute slots
curl -s -X POST $API/hospital/doctors/$DOC_ID/availability \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d "{\"doctor_id\":\"$DOC_ID\",\"weekday\":4,\"start_time\":\"17:00\",\"end_time\":\"20:00\",\"slot_minutes\":30}"

# 5) (Optional) restrict a doctor — e.g. deactivate then re-activate:
curl -s -X PATCH $API/hospital/doctors/$DOC_ID -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" -d '{"status":"active"}'

# 6) Block a period (personal unavailability) inside that window
FRIDAY=$(python -c "from datetime import date,timedelta; d=date.today()+timedelta(days=((4-date.today().weekday())%7 or 7)); print(d.isoformat())")
curl -s -X POST $API/hospital/doctors/$DOC_ID/blocked \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d "{\"doctor_id\":\"$DOC_ID\",\"kind\":\"blocked\",\"start_at\":\"${FRIDAY}T18:00:00\",\"end_at\":\"${FRIDAY}T19:00:00\",\"reason\":\"Team meeting\"}"
# → 201 Created
```

Note: API-created doctors default to `status: "active"` (pass `"status":"invited"` in the
create payload if you want a doctor to start unbookable). Configuration routes (availability,
blocked periods) always work; only the scheduling engine enforces doctor status.

### Step B — Patient discovers REAL availability and books (no AI yet — same engine the AI will call)

```bash
PAT=$(tok patient@demo.health)

# 7) See the slot grid for Dr. Sharma this week (includes unavailable slots + reasons)
curl -s "$API/doctors/$DOC_ID/availability?from_date=$FRIDAY&to_date=$FRIDAY" \
  -H "Authorization: Bearer $PAT"
```

**Expected:** slots at 17:00, 17:30, 19:00, 19:30 marked `"available": true`; the **18:00 and
18:30 slots return `"available": false, "reason": "slot_blocked"`** — proof the blocked period
is respected. Past times show `"reason": "in_past"`.

```bash
# 8) Book 17:00 (slot must exactly match a grid start time)
START="${FRIDAY}T17:00:00"
APPT=$(curl -s -X POST $API/appointments -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" -H "X-Correlation-ID: demo-run-1" \
  -d "{\"doctor_id\":\"$DOC_ID\",\"start_at\":\"$START\",\"appointment_type\":\"in_person\",\"reason\":\"Shoulder pain\"}")
echo "$APPT"
APPT_ID=$(echo "$APPT" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

# 9) Double-booking attempt — same slot, second patient account is not needed;
#    booking the same slot again from any patient fails:
curl -s -o /dev/null -w "%{http_code}\n" -X POST $API/appointments \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -d "{\"doctor_id\":\"$DOC_ID\",\"start_at\":\"$START\"}"
```

**Expected for step 9:** `409` with `{"detail":"...","reason":"slot_already_booked"}`.
The second booking can never succeed — even if two requests arrive at the same instant —
because of the application-level revalidation **plus** the partial unique index
`ux_appointments_active_slot` on `(doctor_id, start_at)` for active appointments.

```bash
# 10) Try an off-grid time (not a configured slot start) → rejected
curl -s -X POST $API/appointments -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d "{\"doctor_id\":\"$DOC_ID\",\"start_at\":\"${FRIDAY}T17:15:00\"}"
# → 409 reason "outside_working_hours"

# 11) Patient's own appointments
curl -s $API/me/appointments -H "Authorization: Bearer $PAT"

# 12) Hospital admin sees the tenant's appointments
curl -s $API/hospital/appointments -H "Authorization: Bearer $ADMIN"

# 13) Cancel (frees the slot)
curl -s -X PATCH $API/appointments/$APPT_ID/cancel -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" -d '{"reason":"Change of plans"}'

# 14) The slot is bookable again
curl -s -X POST $API/appointments -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -d "{\"doctor_id\":\"$DOC_ID\",\"start_at\":\"$START\"}"
# → 201 Created
```

### Step C — Tenant isolation proof

```bash
B_ADMIN=$(tok admin.b@stmary.health)

# St Mary admin cannot see City General's doctors — existence is hidden (404, not 403):
curl -s -o /dev/null -w "%{http_code}\n" \
  "$API/doctors/$DOC_ID/availability" -H "Authorization: Bearer $B_ADMIN"
# → 404

# ...and City General's admin cannot manage St Mary's doctors:
curl -s -o /dev/null -w "%{http_code}\n" \
  -X PATCH $API/hospital/doctors/$DOC_ID -H "Authorization: Bearer $B_ADMIN" \
  -H "Content-Type: application/json" -d '{"status":"inactive"}'
# → 404
```

### Step D — Approval lock (why St Mary is interesting)

```bash
# St Mary is submitted but not approved → its admin can configure doctors? No:
curl -s -X POST $API/hospital/doctors -H "Authorization: Bearer $B_ADMIN" \
  -H "Content-Type: application/json" \
  -d '{"full_name":"Dr. New","specialty":"neurology","consultation_minutes":30}'
# → 409 "Hospital approval required before creating doctors"

# Platform admin approves:
PT=$(tok platform_admin@demo.health)
HOSP_B=$(curl -s $API/platform/hospitals -H "Authorization: Bearer $PT" \
  | python -c "import sys,json; print([h['id'] for h in json.load(sys.stdin) if 'St Mary' in h['name']][0])")
curl -s -X POST "$API/platform/hospitals/$HOSP_B/review?action=approve" -H "Authorization: Bearer $PT"
# Now St Mary's admin can create doctors and publish availability.
```

---

## 6. Expected results — quick reference

| Action | Expected |
|---|---|
| Book a valid on-grid slot | `201` with appointment JSON, `status: "booked"` |
| Book the same slot again (even concurrently) | `409`, `reason: "slot_already_booked"` |
| Book 17:15 when grid is 17:00/17:30/… | `409`, `reason: "outside_working_hours"` |
| Book inside a blocked period | `409`, `reason: "slot_blocked"` |
| Book during the doctor's leave | `409`, `reason: "doctor_on_leave"` |
| Book with an inactive doctor | `409`, `reason: "doctor_inactive"` |
| Book at an unapproved hospital's doctor | `409`, `reason: "hospital_not_approved"` |
| Book in the past / >60 days ahead | `422`, `reason: "in_past"` / `"horizon_exceeded"` |
| Availability search shows blocked slot | `available: false`, `reason: "slot_blocked"` |
| Cross-tenant doctor access | `404` (existence hidden) |
| Cancel a booked appointment | `200`, `status: "cancelled"`; slot becomes bookable again |
| No `Authorization` header | `401` |

## 7. Reset everything

```bash
# stop the server (Ctrl+C), then:
rm -rf data
python -m backend.scripts.seed_demo
python -m uvicorn backend.app.main:app --reload --port 8000
```

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `401` on every request | Missing `.env` or `JWT_SECRET` changed after login → re-run `cp .env.example .env`, restart, log in again |
| Port 8000 busy | `--port 8002` (and adjust `$API` above) |
| `ModuleNotFoundError: backend` | Run all commands from the repository root |
| Booking returns `422 invalid_datetime` | `start_at` must be ISO 8601, e.g. `2026-09-18T17:00:00` |
| Doctor added but can't be booked | Check the doctor's `status` is `active` (`PATCH /hospital/doctors/{id}` with `{"status":"active"}`), the hospital is approved, and availability exists for that weekday |
| Slots missing for a day | Check the doctor has availability rows for that weekday, is active, hospital is approved, and the time isn't in the past |

## 9. What comes next

- **Phase 3 — Mock EHR**: separate FastAPI service with its own database, idempotency keys, fault injection
- **Phase 4 — Booking reliability**: revalidate → EHR call → verify → sync → confirm, with unknown-outcome recovery
- **Phase 5–8**: AI agent + tools, questionnaires, voice, Streamlit dashboards

The scheduling engine you just exercised is exactly what the AI agent will call through
controlled capabilities — the slot grid, validation, and double-booking guarantees stay in
`scheduling/`, never inside AI code.
