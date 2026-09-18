# Healthcare AI Access Platform

A multi-tenant healthcare access platform where hospitals configure doctors and availability,
patients describe what they need in natural language (voice or text), and an AI administrative
assistant finds real availability, books appointments through a controlled capability layer,
synchronizes with an external (Mock) EHR, verifies the result, and only then confirms to the
patient — with pre-visit questionnaires, dashboards, and safe failure recovery.

Built as a 3–4 day prototype following the PRD: *"Autonomous Multi-Hospital Patient Intake,
Scheduling & Pre-Visit Voice Agent"*.

---

## Current status (incremental build)

| Phase | Scope | Status |
|---|---|---|
| 1. Foundation | Project setup, DB models, JWT auth, RBAC, tenant isolation, hospitals, doctors, availability, blocked periods, audit, seed data | ✅ **Done (16 tests green)** |
| 2. Scheduling | Slot engine, appointments, double-booking prevention | ✅ **Done (25 tests green, 41 total)** |
| 3. Mock EHR | Separate service + DB, API-key auth, persistent idempotency, fault injection, connector + sync (`synced/failed/unknown`), operation log | ✅ **Done (32 tests green, 73 total)** — see `README3.md` |
| 4. Booking reliability | Revalidation, EHR verification, stable idempotency keys, unknown-outcome reconciliation (adopt / safe retry), recovery endpoints | ✅ **Done (33 tests green, 106 total)** — see `README4.md` |
| 5. AI agent | LangGraph + capability tools, persisted conversation state, LLM fallback (Gemini → Groq → deterministic), real-availability search, controlled booking, honest EHR reporting | ✅ **Done (28 tests green, 134 total)** — see `README5.md` |
| 6. Questionnaire | Template management, auto-assignment on booking, conversational + form collection, completion gate, doctor view, cancellation propagation, Gemini-first LLM config | ✅ **Done (28 tests green, 162 total)** — see `README6.md` |
| 7. Voice | Web STT → agent → TTS (text fallback always), Gemini-first (no Groq needed), audio provenance in the event log, browser demo page | ✅ **Done (27 tests green, 189 total)** — see `README7.md` |
| 8. Dashboards | Streamlit: patient / doctor / hospital admin / platform admin | Planned |

Everything below runs **today** with Phases 1–5. For a guided two-service walkthrough
(book → sync → verify → failure demos), see **[README3.md](README3.md)**; for the Phase 4
failure-recovery demonstrations (server error, unknown-adopt, unknown-retry), see
**[README4.md](README4.md)**; for the Phase 5 conversational agent walkthrough, see
**[README5.md](README5.md)**; for the Phase 7 voice walkthrough (TTS→STT round trip, browser demo), see
**[README7.md](README7.md)**; for Phase 1–2 configuration and scheduling walkthrough, see
**[README2.md](README2.md)**; agent architecture: **[docs/ai-agent.md](docs/ai-agent.md)**; voice architecture:
**[docs/voice.md](docs/voice.md)**.

---

## Quick start

### Prerequisites

- **Python 3.10+**
- That's it for local development: the app uses SQLite (auto-created) and only packages
  that are commonly pre-installed. If your machine has internet access, install the pinned
  dependency list first (recommended):

```bash
python -m venv .venv
# Windows (Git Bash):  source .venv/Scripts/activate
# macOS/Linux:         source .venv/bin/activate
pip install -r requirements.txt
```

> Note: this prototype was developed in an environment **without PyPI access**, so the code
> deliberately avoids exotic dependencies: JWT (HS256) is implemented on the standard library
> (`hmac`/`hashlib`), passwords use `bcrypt` directly, validation uses Pydantic + small regex
> helpers, and tests run on `unittest` (no pytest needed).

### 1. Configure environment

```bash
cp .env.example .env     # Windows PowerShell: copy .env.example .env
```

Edit `.env` — for local development the defaults work. Set at least:

```ini
JWT_SECRET=change-me-to-a-long-random-string
DATABASE_URL=sqlite:///./data/platform.db
```

Never commit real secrets — `.env` is git-ignored.

### 2. Seed demo data

```bash
python -m backend.scripts.seed_demo
```

This creates two hospitals (City General — **approved** and fully configured; St Mary —
submitted/pending approval), doctors with weekly availability, a leave period, and all demo
accounts. The script is idempotent — run it as many times as you like.

### 3. Run the API

```bash
python -m uvicorn backend.app.main:app --reload --port 8000
```

- Interactive API docs: **http://127.0.0.1:8000/docs**
- Health check: http://127.0.0.1:8000/health

### 4. Run the tests

```bash
python -m unittest discover -s tests -v    # 189 tests (Phases 1–7)
```

Covers: registration/login, RBAC, hospital approval lifecycle, doctor + availability
management, validation errors, and **tenant isolation** (Hospital B can never see or modify
Hospital A's doctors — it gets `404`, not `403`, so existence is hidden); scheduling rules
and double-booking races; Phase 3's EHR API-key auth, persistent idempotency, database
separation, connector error mapping, sync outcomes (`synced/failed/unknown`) and fault
injection; Phase 4's revalidation, EHR verification before success, stable idempotency
keys, unknown-outcome reconciliation (adopt + safe retry), recovery-idempotency and
tenant-scoped recovery endpoints; and Phase 5's slot-filling state machine, capability
authorization, conversational booking end-to-end (search → select → book → EHR outcome),
restart-safe conversation persistence and agent RBAC/isolation. Runbooks:
**[README3.md](README3.md)**, **[README4.md](README4.md)**, **[README5.md](README5.md)**;
recovery design: **[docs/failure-recovery.md](docs/failure-recovery.md)**; agent design:
**[docs/ai-agent.md](docs/ai-agent.md)**.

---

## Demo accounts

Password for **all** seeded accounts: `Demo1234!`

| Email | Role | Notes |
|---|---|---|
| `platform_admin@demo.health` | Platform admin | Approves/rejects hospitals, views audit |
| `admin.a@citygeneral.health` | Hospital admin | City General Hospital (approved) |
| `admin.b@stmary.health` | Hospital admin | St Mary Hospital (submitted — locked until approved) |
| `dr.rao@citygeneral.health` | Doctor | Orthopedics, Mon–Fri 09:00–13:00 & 14:00–17:00 |
| `dr.mehta@citygeneral.health` | Doctor | Cardiology, Mon/Wed/Fri 10:00–13:00 |
| `dr.iyer@stmary.health` | Doctor | Orthopedics (hospital not yet approved) |
| `patient@demo.health` | Patient | Demo patient profile |

---

## 5-minute API tour (curl)

```bash
# 1) Login as hospital admin (City General)
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin.a@citygeneral.health","password":"Demo1234!"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# 2) My hospital + my doctors (tenant-scoped)
curl -s http://127.0.0.1:8000/hospital/me -H "Authorization: Bearer $TOKEN"
curl -s http://127.0.0.1:8000/hospital/doctors -H "Authorization: Bearer $TOKEN"

# 3) Add a doctor + weekly availability (only works because hospital is approved)
DOCTOR_ID=$(curl -s -X POST http://127.0.0.1:8000/hospital/doctors \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"full_name":"Dr. Sharma","specialty":"orthopedics","consultation_minutes":30}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

curl -s -X POST http://127.0.0.1:8000/hospital/doctors/$DOCTOR_ID/availability \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"doctor_id\":\"$DOCTOR_ID\",\"weekday\":4,\"start_time\":\"17:00\",\"end_time\":\"20:00\",\"slot_minutes\":30}"

# 4) Block a period (or set leave)
curl -s -X POST http://127.0.0.1:8000/hospital/doctors/$DOCTOR_ID/blocked \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"doctor_id\":\"$DOCTOR_ID\",\"kind\":\"leave\",\"start_at\":\"2026-09-25T09:00:00\",\"end_at\":\"2026-09-25T17:00:00\",\"reason\":\"CME training\"}"

# 5) Platform admin approves the pending hospital
PTOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"platform_admin@demo.health","password":"Demo1234!"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
curl -s http://127.0.0.1:8000/platform/hospitals -H "Authorization: Bearer $PTOKEN"
curl -s -X POST "http://127.0.0.1:8000/platform/hospitals/<hospital_id>/review?action=approve" \
  -H "Authorization: Bearer $PTOKEN"

# 6) Tenant isolation proof: admin.b cannot see City General's doctor
BTOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin.b@stmary.health","password":"Demo1234!"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
curl -s http://127.0.0.1:8000/hospital/doctors -H "Authorization: Bearer $BTOKEN"   # -> []
```

---

## Architecture (Phase 1)

```
Streamlit dashboards (Phase 8)        Mock EHR (Phase 3, separate process + DB)
        │                                     ▲
        ▼ HTTP/JWT                            │ HTTP via connector interface
FastAPI API  ──►  AI agent (Phase 5) ──► Capabilities (validation, authz, idempotency)
        │                                     ▼
        ▼                        Services: Scheduling / Appointment / Questionnaire
SQLAlchemy models ── SQLite (default) or PostgreSQL
```

**Strict boundaries** (enforced as phases land): the AI never touches the database or EHR
directly — it calls capabilities; scheduling never depends on the AI; the Mock EHR sits behind
a connector interface so a real EHR connector can replace it.

### Project structure

```
backend/
└─ app/
   ├─ main.py               # FastAPI entrypoint (tables auto-created)
   ├─ core/                 # config (env), security (bcrypt + stdlib HS256 JWT), validators
   ├─ database/             # engine/session wiring, Base
   ├─ models/               # User, Hospital, Doctor, DoctorAvailability, BlockedPeriod,
   │                        # Patient, AuditEvent
   ├─ schemas/              # Pydantic request/response models
   ├─ auth/                 # JWT resolution + RBAC guards
   ├─ audit/                # append-only, privacy-aware audit helper
   └─ api/routes/           # auth, hospital (tenant-scoped), platform, patient
backend/scripts/seed_demo.py
tests/                      # unittest suite (python -m unittest discover -s tests)
docs/                       # architecture / ai / integration / failure-recovery (as phases land)
```

### Data model highlights

- **Tenancy**: every tenant-owned row carries `hospital_id`; queries are always filtered by the
  caller's hospital; cross-hospital access returns `404` (hidden, not forbidden).
- **Hospital lifecycle**: `draft → submitted → approved / rejected (+ suspended)`. Only approved
  hospitals can create active doctors or publish availability.
- **Doctor lifecycle**: `invited / active / inactive`; only active doctors with availability can
  receive appointments (enforced by the scheduling engine in Phase 2).
- **Audit**: every login, configuration change and administrative action is recorded
  (`audit_events`) with actor, role, hospital, resource and correlation id — viewable by the
  platform admin at `GET /platform/audit`.

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/platform.db` | Platform DB (PostgreSQL-ready) |
| `JWT_SECRET` | dev default — **change it** | Token signing key |
| `JWT_EXPIRES_MINUTES` | `720` | Token lifetime |
| `MOCK_EHR_URL` | `http://127.0.0.1:8001` | Mock EHR base URL (Phase 3) |
| `MOCK_EHR_DATABASE_URL` | `sqlite:///./data/mock_ehr.db` | Mock EHR's own DB |
| `MOCK_EHR_API_KEY` | dev default | Shared secret for connector auth |
| `MOCK_EHR_TIMEOUT_SECONDS` | `5` | Connector timeout (must be BELOW the EHR fault delay to demo unknown outcomes) |
| `MOCK_EHR_FAULT_MODE` / `_DELAY_SECONDS` / `_PATHS` | `none` / `10` / — | Deterministic fault injection (see `docs/integration.md`) |
| `GROQ_API_KEY` / `GEMINI_API_KEY` | — | AI providers (Gemini primary; Groq optional fallback) |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Agent chat LLM |
| `STT_PROVIDER` | `gemini` | Voice STT provider — `gemini` (default) or `groq` (Whisper) |
| `STT_MODEL` | `gemini-2.5-flash` | Voice STT model (own quota bucket, not the chat model) |
| `WHISPER_MODEL` | `whisper-large-v3-turbo` | Whisper model (only when `STT_PROVIDER=groq`) |
| `TTS_MODEL` / `TTS_VOICE_NAME` | `gemini-2.5-flash-preview-tts` / `Zephyr` | Voice TTS (best-effort; text fallback always) |
| `API_BASE_URL` | `http://127.0.0.1:8000` | Used by the Streamlit frontend |

Full template: [`.env.example`](.env.example).

---

## Deploy to Render (demo)

The repo ships **two** independently deployable ASGI apps and a Render blueprint
(`render.yaml`) describing both:

| Service | Start command (Render injects `$PORT`) | Health check |
|---|---|---|
| Platform API | `uvicorn backend.app.main:app --host 0.0.0.0 --port $PORT` | `/health` |
| Mock EHR | `uvicorn mock_ehr.app.main:app --host 0.0.0.0 --port $PORT` | `/health` |

**Two options:**

1. **Blueprint (recommended):** Render dashboard → *New* → *Blueprint* → pick this
   repo. Render reads `render.yaml`, creates both services, and wires
   `MOCK_EHR_URL` from the EHR service automatically. You'll be prompted once for
   the secret env vars (`sync: false` in the blueprint).
2. **Manual:** create two *Web Service*s from the repo with the start commands
   above, then set the env vars listed in `render.yaml` by hand. Point the
   platform's `MOCK_EHR_URL` at the EHR service's public URL
   (`https://<ehr-service>.onrender.com`).

**Required env vars (set in the Render dashboard — never in git):**
`MOCK_EHR_API_KEY` (same value on BOTH services), `JWT_SECRET`,
`GEMINI_API_KEY`. Sensible defaults for everything else are in `render.yaml`
and `.env.example`. `GROQ_API_KEY` is NOT required (Gemini-first everywhere).

**Demo caveat:** Render free instances have an **ephemeral disk** — the SQLite
files under `data/` (both apps create the directory and schema on boot) reset on
redeploy/restart, and free services spin down after inactivity (first request
after a nap is slow). Run the seed script's equivalent data setup by visiting the
platform once after deploy, or upgrade to a paid disk for persistence. Both apps
read config exclusively from environment variables on Render — a local `.env`
file is never needed in deployment (it is git-ignored and never committed).

---

## Troubleshooting

- **Port already in use** → run on another port: `--port 8002`.
- **Reset the database** → stop the server, delete `data/`, then re-run the seed script.
- **401 on every request** → you skipped `cp .env.example .env`, or changed `JWT_SECRET`
  after issuing tokens (old tokens become invalid — log in again).
- **409 "requires platform approval"** → the hospital hasn't been approved yet; log in as the
  platform admin and approve it (see API tour step 5).
- **Windows**: run commands from Git Bash (the repo's scripts and curl examples assume POSIX shell).

## Known limitations (prototype scope)

- Phase 8 (dashboards) is pending — see the status table above. Recovery is endpoint/service-driven; a
  background scheduler for `sweep_unknown` is a natural future addition.
- SQLite by default (WAL mode, FK pragma on); PostgreSQL supported via `DATABASE_URL` but not
  exercised in CI.
- No HTTPS/deployment hardening in this phase; CORS is open for local development.
- Secrets are read from environment only; no key material exists in the repository.
