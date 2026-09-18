# Phase 6 — Pre-Visit Questionnaires: Setup, API & Manual Walkthrough

Phase 6 adds the third pillar of the platform workflow: after a booking, the
right pre-visit questionnaire is **auto-assigned**, the AI agent **collects the
answers conversationally** (or the patient fills them in via the API), and the
**doctor sees structured answers** before the visit.

> Also covered here: switching the agent's LLM to **Gemini** (`GEMINI_API_KEY`
> / `GEMINI_MODEL` — Gemini is now preferred over Groq).

---

## 1. Setup

```bash
# from the repo root
python -m backend.scripts.seed_demo        # idempotent; also seeds questionnaire templates
```

Terminal 1 — Mock EHR:

```bash
python -m uvicorn mock_ehr.app.main:app --host 127.0.0.1 --port 8001
```

Terminal 2 — Platform API:

```bash
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8002
```

### LLM configuration (Gemini)

`.env` (no keys hardcoded anywhere in code):

```
GEMINI_API_KEY=<your key>
GEMINI_MODEL=gemini-3.6-flash     # gemini-2.0-flash is retired (404 NOT_FOUND)
# GROQ_API_KEY=...                # optional fallback; used only if GEMINI_API_KEY is absent
# LLM_PROVIDER=gemini             # optional explicit override ("gemini" | "groq")
```

Provider selection order in `backend/app/agent/llm.py`:

1. `LLM_PROVIDER` override (if it fails to initialize, falls through)
2. Gemini — when `GEMINI_API_KEY` is set
3. Groq — when `GROQ_API_KEY` is set
4. None — the agent runs the deterministic fallback dialogue (never crashes)

> Free-tier note: `gemini-3.6-flash` allows ~20 requests/minute. If the quota
> is exhausted the agent transparently continues with the deterministic
> dialogue; the demo still works.

---

## 2. Manual walkthrough (Git Bash)

```bash
API=http://127.0.0.1:8002
EHR=http://127.0.0.1:8001
H='-Content-Type: application/json'

# ---- login helpers -------------------------------------------------
PTOK=$(curl -s -X POST $API/auth/login $H \
  -d '{"email":"patient@demo.health","password":"Demo1234!"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
ATOK=$(curl -s -X POST $API/auth/login $H \
  -d '{"email":"admin.a@citygeneral.health","password":"Demo1234!"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
DTOK=$(curl -s -X POST $API/auth/login $H \
  -d '{"email":"dr.rao@citygeneral.health","password":"Demo1234!"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
```

### 2a. Hospital admin creates a questionnaire template

```bash
curl -s -X POST $API/questionnaires/templates \
  -H "Authorization: Bearer $ATOK" $H \
  -d '{"name":"Cardiology Intake","kind":"specialty","questions":[
        {"key":"chest_pain","kind":"boolean","prompt":"Any chest pain?","required":true},
        {"key":"duration","kind":"single_choice","prompt":"For how long?",
         "options":["days","weeks","months"],"required":true}]}' | python -m json.tool

curl -s $API/questionnaires/templates \
  -H "Authorization: Bearer $ATOK" | python -c "import sys,json;[print(t['name'],t['kind']) for t in json.load(sys.stdin)]"
```

### 2b. Book an appointment (auto-assigns questionnaires)

Pick a free Monday slot for Dr. Rao (Mon–Fri 09:00–13:00, 14:00–17:00):

```bash
curl -s "$API/doctors/<DOCTOR_ID>/availability" -H "Authorization: Bearer $PTOK"

APPT=$(curl -s -X POST $API/appointments \
  -H "Authorization: Bearer $PTOK" $H \
  -d '{"doctor_id":"<DOCTOR_ID>","start_at":"<YYYY-MM-DDT09:00:00>","appointment_type":"in_person"}')
echo "$APPT" | python -m json.tool
APPT_ID=$(echo "$APPT" | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
```

The booking response is unchanged (Phase 2 shape) — questionnaires are assigned
silently and idempotently in the background.

### 2c. Patient lists and answers the forms

```bash
curl -s $API/questionnaires/appointments/$APPT_ID/questionnaires \
  -H "Authorization: Bearer $PTOK" | python -m json.tool
# -> two assignments: the orthopedics (specialty) form + the standard form
ASSIGN_ID=<id of one assignment>

curl -s -X POST $API/questionnaires/assignments/$ASSIGN_ID/answers \
  -H "Authorization: Bearer $PTOK" $H \
  -d '{"key":"joint","value":"Shoulder"}' | python -m json.tool

# batch (form-style) submission
curl -s -X POST $API/questionnaires/assignments/$ASSIGN_ID/answers:batch \
  -H "Authorization: Bearer $PTOK" $H \
  -d '{"answers":{"injury":false,"mobility":"Slightly limited"}}' | python -m json.tool

# invalid answer -> 422 with the rule in the message
curl -s -X POST $API/questionnaires/assignments/$ASSIGN_ID/answers \
  -H "Authorization: Bearer $PTOK" $H \
  -d '{"key":"severity","value":11}'

# complete (fails 422 while a required question is unanswered)
curl -s -X POST $API/questionnaires/assignments/$ASSIGN_ID/complete \
  -H "Authorization: Bearer $PTOK"
```

### 2d. Doctor views structured answers

```bash
curl -s "$API/questionnaires/doctor/mine?status=pending" \
  -H "Authorization: Bearer $DTOK" | python -m json.tool

curl -s $API/questionnaires/assignments/$ASSIGN_ID \
  -H "Authorization: Bearer $DTOK" | python -m json.tool
# answers + template questions + patient/appointment context
```

### 2e. Conversational collection through the agent

```bash
CONV=$(curl -s -X POST $API/agent/sessions -H "Authorization: Bearer $PTOK" | python -c "import sys,json;print(json.load(sys.stdin)['id'])")

say() { curl -s -X POST $API/agent/sessions/$CONV/messages \
  -H "Authorization: Bearer $PTOK" $H -d "{\"message\":\"$1\"}" | python -m json.tool; }

say "I need to see an orthopedic doctor for my shoulder pain sometime this week"
say "in person"
say "1"                      # picks a slot -> booked + EHR sync + first questionnaire question
say "Shoulder"               # answers 'joint'  (specialty form completes -> standard form chains in)
say "shoulder pain"          # answers 'reason'
say "no"                     # answers 'fever'
say "4"                      # answers 'severity' -> form completed
```

Each booking reply ends with the first questionnaire question; each answer
reply either asks the next question (with progress `n/m`) or confirms
completion. Answers are stored structurally and visible to the doctor
immediately (`/questionnaires/doctor/mine`).

### 2f. Cancellation guard

```bash
curl -s -X PATCH $API/appointments/$APPT_ID/cancel \
  -H "Authorization: Bearer $PTOK" $H -d '{"reason":"schedule conflict"}'
# pending questionnaires for this appointment are cancelled automatically
```

---

## 3. API summary

| Method | Path | Who |
|---|---|---|
| POST | `/questionnaires/templates` | hospital admin |
| GET | `/questionnaires/templates` | hospital admin (own tenant) |
| POST | `/questionnaires/appointments/{id}/questionnaires` | patient owner / tenant admin / platform admin (idempotent) |
| GET | `/questionnaires/appointments/{id}/questionnaires` | same scoping |
| POST | `/questionnaires/assignments/{id}/answers` | patient owner |
| POST | `/questionnaires/assignments/{id}/answers:batch` | patient owner |
| POST | `/questionnaires/assignments/{id}/complete` | patient owner (mandatory gate) |
| GET | `/questionnaires/assignments/{id}` | patient owner / assignment's doctor / tenant admin / platform admin |
| GET | `/questionnaires/doctor/mine?status=` | doctor (own assignments) |

Cross-tenant reads return **404** (existence hidden), per the platform convention.

---

## 4. Tests

```bash
python -m unittest discover -s tests -v
```

Current result: **Ran 162 tests — OK** (134 from Phases 1–5 + 28 new Phase 6
tests: templates, auto-assignment, answer validation, completion gate, doctor
view, cancellation propagation, conversational collection, RBAC/isolation).

---

## 5. Design notes

- **Assignment is idempotent** — unique per (appointment, template); the
  booking path, the agent path and the manual endpoint can all call it.
- **Answers are validated against the template** before storage (kinds:
  `free_text`, `boolean`, `single_choice`, `multiple_choice`, `scale_1_10`);
  conversational coercion ("yes"/"no", option-word matching, 1–10 integers)
  happens in the agent layer, never in the LLM.
- **Completion gate** — `complete` refuses (422) until every `required`
  question has an answer; completed forms are closed (409) to further edits.
- **Cancellation propagates** in the same transaction as the appointment
  cancel; completed forms are preserved.
- **The AI never touches the DB/EHR directly** — it uses the same services as
  the REST API via the Phase 5 capability boundary.
